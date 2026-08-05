import json
import logging
import re
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from functools import wraps

import markdown
import requests
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Prefetch, ProtectedError, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views.decorators.csrf import csrf_exempt

from . import ai_coach as ai_coach_client
from . import bunny, certificates, emails, paymob
from .access import get_or_create_enrollment, student_has_access
from .forms import (
    ChoiceForm, CourseCreationForm, GradeForm, InstructorSignUpForm, LectureForm, ModuleForm,
    PayoutRequestForm, ProfileForm, QuestionForm, QuizForm, ResourceForm, ReviewForm,
    StudentSignUpForm, SubmissionForm, TrackForm, TrackRequestForm,
)
from .models import (
    AIConversation, AIMessage, Certificate, Choice, Course, Enrollment, InstructorWallet,
    Lecture, LectureProgress, LegalDocument, Module, Payment, Payout, Plan, Question, Quiz,
    QuizAnswer, QuizAttempt, Resource, RevenueDistribution, Review, Subscription,
    SubscriptionPeriod, Submission, Track, TrackRequest, TrackRoadmapStep, User, WalletTransaction,
    WatchEvent,
)

logger = logging.getLogger(__name__)


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            return redirect('platform_home')
        return view_func(request, *args, **kwargs)
    return wrapper


def _generate_poster_safely(course):
    """Course.generate_poster() involves a storage upload -- never let a
    hiccup there (or a misconfigured storage backend) turn an otherwise
    successful course create/edit/view into a hard error. The template
    already copes with a missing poster_image by simply not showing the
    cover screen, and it's retried on the next save or lecture view."""
    try:
        course.generate_poster()
    except Exception:
        logger.warning('Failed to generate poster for course id=%s', course.id, exc_info=True)

# Only overrides where a plain login (no ?next=, e.g. submitting the form
# directly from /login/) lands -- an explicit ?next= from being bounced off
# a protected page always still wins, same as Django's own LoginView.
# Without this, an approved instructor logging in landed on the generic
# marketing homepage ("Start Learning" / "Become an Instructor" CTAs aimed
# at signed-out visitors) instead of the dashboard the approval email
# itself promises ("You now have access to your instructor dashboard").
class RoleAwareLoginView(auth_views.LoginView):
    def get_default_redirect_url(self):
        if self.request.user.is_authenticated and self.request.user.is_instructor:
            return resolve_url('instructor_dashboard')
        return super().get_default_redirect_url()


# 1. Platform Homepage
def platform_home(request):
    tracks = Track.objects.filter(parent__isnull=True, is_active=True)
    plans = Plan.objects.filter(is_active=True)
    return render(request, 'platform_home.html', {'tracks': tracks, 'plans': plans})


def _legal_document(slug):
    return get_object_or_404(
        LegalDocument.objects.prefetch_related('sections'), slug=slug)


def terms(request):
    return render(request, 'legal/document.html', {'document': _legal_document('terms')})


def privacy(request):
    return render(request, 'legal/document.html', {'document': _legal_document('privacy')})

# Profile settings -- currently just the avatar, open to any authenticated
# user regardless of role.
@login_required
def profile(request):
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Profile updated.'))
            return redirect('profile')
    else:
        form = ProfileForm(instance=request.user)
    return render(request, 'dashboard/profile.html', {'form': form})


# 2. Student Sign Up View
def student_signup(request):
    if request.method == 'POST':
        form = StudentSignUpForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
            except IntegrityError:
                # clean_username()/clean_email() already checked for
                # duplicates, but that check-then-save isn't atomic -- two
                # near-simultaneous submits (double-click, a slow request
                # retried by the browser) can both pass validation and only
                # collide at the DB's unique constraint. Surface it as a
                # normal form error instead of a raw 500.
                form.add_error(
                    None, _('That username was just taken by someone else. Please try again.'))
            else:
                emails.send_welcome_email(user)
                emails.send_student_signup_notification(user)
                # Unlike instructor_signup below, a student account needs no
                # admin review (see StudentSignUpForm.save()) -- sign them
                # in immediately instead of bouncing them to the login form
                # for an account that's already usable.
                auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                messages.success(request, _('Welcome to Mendoura! Your account is ready.'))
                return redirect('platform_home')
    else:
        form = StudentSignUpForm()
    return render(request, 'registration/signup_student.html', {'form': form})

# 3. Instructor Sign Up View
def instructor_signup(request):
    if request.method == 'POST':
        form = InstructorSignUpForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
            except IntegrityError:
                # Same race as student_signup() above.
                form.add_error(
                    None, _('That username was just taken by someone else. Please try again.'))
            else:
                # The full Instructor welcome email promises dashboard access,
                # which only becomes true once an admin approves the account
                # (sent from approve_user() instead) -- this lighter
                # "application received" email is safe to send right away.
                emails.send_instructor_application_received_email(user)
                emails.send_instructor_application_notification(user)
                messages.success(
                    request,
                    _("Your account has been created and is pending administrator approval. "
                      "You'll be able to log in once it's approved."))
                return redirect('login')
    else:
        form = InstructorSignUpForm()
    return render(request, 'registration/signup_instructor.html', {'form': form})

# 4. Instructor Dashboard View
@login_required
def instructor_dashboard(request):
    if not request.user.is_instructor:
        return redirect('platform_home')
    courses = Course.objects.filter(instructor=request.user).order_by('-created_at')
    wallet, _created = InstructorWallet.objects.get_or_create(instructor=request.user)
    recent_sales = (Payment.objects.filter(course__instructor=request.user,
                                            status=Payment.Status.SUCCEEDED)
                     .select_related('course', 'student').order_by('-created_at')[:5])
    total_students = Enrollment.objects.filter(course__instructor=request.user).count()

    return render(request, 'dashboard/instructor.html', {
        'courses': courses,
        'wallet': wallet,
        'recent_sales': recent_sales,
        'total_students': total_students,
    })

# 5. Create Course View -- Step 1 of the guided course-creation wizard.
# production_type is chosen here and is read-only once the course has its
# first successful sale (enforced in Course.save()).
@login_required
def create_course(request):
    if not request.user.is_instructor:
        return redirect('platform_home')
    if request.method == 'POST':
        form = CourseCreationForm(request.POST, request.FILES)
        if form.is_valid():
            course = form.save(commit=False)
            course.instructor = request.user
            course.save()
            _generate_poster_safely(course)
            return redirect('course_wizard_modules', course_id=course.id)
    else:
        form = CourseCreationForm()
    return render(request, 'dashboard/create_course.html', {
        'form': form, 'wizard_step_choices': WIZARD_STEP_CHOICES, 'current_step': 1,
    })


# ---------------------------------------------------------------------------
# Course creation wizard -- Steps 2-4. Step 1 is create_course() above.
#
# No new "current step" field on Course: the step to show/resume is always
# derived from what's actually in the database (module count, whether each
# module's lecture has a video/script yet) rather than a separately tracked
# pointer that could drift out of sync with the real content. Only usable
# while the course is still a Draft -- once submitted for review the normal
# Curriculum/Edit pages (manage_modules, edit_course, ...) take over, same
# as before this wizard existed.
# ---------------------------------------------------------------------------

WIZARD_STEP_CHOICES = [
    (1, _lazy('Details')), (2, _lazy('Modules')), (3, _lazy('Content')), (4, _lazy('Review')),
]


def _get_draft_course_or_redirect(request, course_id):
    """Shared ownership + draft-status guard for every wizard step. Returns
    (course, None) on success, or (None, redirect_response) if the caller
    should bail out and return that response instead."""
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    if course.status != Course.Status.DRAFT:
        # Already submitted/published/rejected -- the wizard is only for
        # first-time setup. Send them to the normal editing tools instead.
        return None, redirect('manage_modules', course_id=course.id)
    return course, None


def _wizard_module_lecture(module):
    """The wizard treats each module as having exactly one primary lecture
    (its video or script) -- simpler to build/understand step 3 around than
    the full multi-lecture-per-module editor, which stays available via
    Curriculum -> Manage Lectures after creation for anyone who wants more
    than one lecture per module. Auto-created the first time this module's
    wizard step is opened, titled after the module until the instructor
    renames it."""
    lecture = module.lectures.first()
    if lecture is None:
        lecture = Lecture.objects.create(module=module, title=module.title)
    return lecture


def _sync_bunny_status(lecture):
    """Bunny's webhook (bunny_webhook above) is the fast path for updating
    bunny_status, but webhook delivery isn't guaranteed -- e.g. a Render
    free-tier dyno asleep when Bunny calls back -- and nothing ever retries
    it, so a lecture can stay stuck showing "still processing" long after
    Bunny actually finished. Called whenever we're about to show this status
    to an instructor, so it reflects Bunny's real current state instead of a
    possibly-stale local flag. Also backfills duration_seconds the first time
    Bunny reports a real length -- nothing else in the upload flow ever
    populates it. Best-effort: a Bunny hiccup here should never break the
    page that's just trying to display a status.
    [BUNNY_STATUS_DEBUG]"""
    if not lecture.bunny_video_id:
        return
    if lecture.bunny_ready and lecture.duration_seconds:
        return
    try:
        info = bunny.get_video_info(lecture.bunny_video_id)
    except (bunny.BunnyError, requests.RequestException):
        logger.error(
            '[BUNNY_STATUS_DEBUG] _sync_bunny_status failed to refresh lecture_id=%s guid=%s',
            lecture.id, lecture.bunny_video_id, exc_info=True)
        return

    status = info['status']
    update_fields = []
    if status != lecture.bunny_status:
        logger.info(
            '[BUNNY_STATUS_DEBUG] _sync_bunny_status updating lecture_id=%s guid=%s: %s -> %s',
            lecture.id, lecture.bunny_video_id, lecture.bunny_status, status)
        lecture.bunny_status = status
        update_fields.append('bunny_status')
    if not lecture.duration_seconds and info['length']:
        lecture.duration_seconds = info['length']
        update_fields.append('duration_seconds')
    if update_fields:
        lecture.save(update_fields=update_fields)


def _module_content_ready(course, module):
    """Whether this module's required video/script has been provided --
    gates the wizard's Next Module/Finish action. Checked live against the
    actual data instead of a stored flag, so it can never disagree with
    what's really there."""
    lecture = module.lectures.first()
    if lecture is None:
        return False
    if course.production_type == Course.ProductionType.SCRIPT_ONLY:
        return bool(lecture.ai_generated_script and lecture.ai_generated_script.strip())
    return bool(lecture.bunny_video_id or lecture.video_url)


def _first_incomplete_module(course):
    """First module (in order) still missing its video/script, or None if
    every module is ready -- this is what "resume where I left off" and the
    Step 2 -> Step 3 handoff both use to pick which module to land on."""
    for module in course.modules.order_by('order', 'id'):
        if not _module_content_ready(course, module):
            return module
    return None


@login_required
def course_wizard_resume(request, course_id):
    """Landing point for "Continue Setup" on the instructor dashboard --
    figures out where an in-progress draft left off and sends the
    instructor straight there instead of making them re-discover it."""
    course, bail = _get_draft_course_or_redirect(request, course_id)
    if bail:
        return bail
    if not course.modules.exists():
        return redirect('course_wizard_modules', course_id=course.id)
    next_module = _first_incomplete_module(course)
    if next_module:
        return redirect('course_wizard_module_content', course_id=course.id, module_id=next_module.id)
    return redirect('course_wizard_review', course_id=course.id)


# Step 2 -- Modules. Self-contained (not manage_modules reused) so the
# "Next" hand-off to Step 3 and the wizard's step-progress header can live
# here without changing the always-available Curriculum page instructors
# use to manage modules after creation too.
@login_required
def course_wizard_modules(request, course_id):
    course, bail = _get_draft_course_or_redirect(request, course_id)
    if bail:
        return bail
    modules = course.modules.order_by('order', 'id')

    if request.method == 'POST':
        action = request.POST.get('action', 'add')
        if action == 'delete':
            module = get_object_or_404(Module, id=request.POST.get('module_id'), course=course)
            module.delete()
        else:
            form = ModuleForm(request.POST)
            if form.is_valid():
                module = form.save(commit=False)
                module.course = course
                if not module.order:
                    module.order = modules.count() + 1
                module.save()
        return redirect('course_wizard_modules', course_id=course.id)

    return render(request, 'dashboard/wizard_modules.html', {
        'course': course, 'modules': modules, 'form': ModuleForm(),
        'wizard_step_choices': WIZARD_STEP_CHOICES, 'current_step': 2,
    })


# Step 3 -- per module, in sequence: video/script, then an optional quiz
# (with its questions and each question's answer choices, all on this one
# page instead of split across manage_quiz.html + edit_question.html), then
# Skip Quiz / Next Module / Finish. One view handling several POST actions
# via a hidden `action` field -- every action redirects back to this same
# step so the instructor is never bounced out of the wizard, the way
# reusing add_question/add_choice/etc. (which redirect to the classic
# Curriculum pages) would.
@login_required
def course_wizard_module_content(request, course_id, module_id):
    course, bail = _get_draft_course_or_redirect(request, course_id)
    if bail:
        return bail
    module = get_object_or_404(Module, id=module_id, course=course)
    lecture = _wizard_module_lecture(module)
    if request.method == 'GET':
        _sync_bunny_status(lecture)
    quiz = getattr(module, 'quiz', None)
    modules = list(course.modules.order_by('order', 'id'))
    module_index = next((i for i, m in enumerate(modules) if m.id == module.id), 0)
    is_last_module = module_index == len(modules) - 1

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save_video_url':
            lecture.video_url = request.POST.get('video_url', '').strip()
            lecture.save(update_fields=['video_url'])

        elif action == 'save_script':
            lecture.ai_generated_script = request.POST.get('script', '').strip()
            lecture.save(update_fields=['ai_generated_script'])

        elif action == 'save_quiz_settings':
            form = QuizForm(request.POST, instance=quiz)
            if form.is_valid():
                quiz = form.save(commit=False)
                quiz.module = module
                quiz.save()

        elif action == 'add_question' and quiz:
            form = QuestionForm(request.POST)
            if form.is_valid():
                question = form.save(commit=False)
                question.quiz = quiz
                question.save()

        elif action == 'delete_question':
            Question.objects.filter(id=request.POST.get('question_id'), quiz=quiz).delete()

        elif action == 'add_choice':
            question = get_object_or_404(Question, id=request.POST.get('question_id'), quiz=quiz)
            form = ChoiceForm(request.POST)
            if form.is_valid():
                choice = form.save(commit=False)
                choice.question = question
                choice.save()
                if choice.is_correct:
                    question.choices.exclude(id=choice.id).update(is_correct=False)

        elif action == 'mark_choice_correct':
            question = get_object_or_404(Question, id=request.POST.get('question_id'), quiz=quiz)
            choice = get_object_or_404(Choice, id=request.POST.get('choice_id'), question=question)
            question.choices.update(is_correct=False)
            choice.is_correct = True
            choice.save(update_fields=['is_correct'])

        elif action == 'delete_choice':
            Choice.objects.filter(id=request.POST.get('choice_id'), question__quiz=quiz).delete()

        elif action == 'delete_quiz' and quiz:
            quiz.delete()
            quiz = None

        elif action == 'advance':
            if not _module_content_ready(course, module):
                error_message = (
                    _('Add your script for this module before continuing.')
                    if course.production_type == Course.ProductionType.SCRIPT_ONLY
                    else _('Upload a video (or add an external video link) for this module before continuing.')
                )
                messages.error(request, error_message)
                return redirect('course_wizard_module_content', course_id=course.id, module_id=module.id)
            if is_last_module:
                return redirect('course_wizard_review', course_id=course.id)
            return redirect('course_wizard_module_content', course_id=course.id, module_id=modules[module_index + 1].id)

        return redirect('course_wizard_module_content', course_id=course.id, module_id=module.id)

    questions = quiz.questions.prefetch_related('choices') if quiz else []
    return render(request, 'dashboard/wizard_module_content.html', {
        'course': course, 'module': module, 'lecture': lecture, 'quiz': quiz,
        'questions': questions, 'quiz_form': QuizForm(instance=quiz),
        'question_form': QuestionForm(), 'choice_form': ChoiceForm(),
        'bunny_configured': bunny.is_configured(),
        'module_index': module_index, 'module_count': len(modules), 'is_last_module': is_last_module,
        'content_ready': _module_content_ready(course, module),
        'wizard_step_choices': WIZARD_STEP_CHOICES, 'current_step': 3,
    })


# Step 4 -- summary + the same Draft/Rejected -> Pending Review transition
# toggle_publish() already does for the classic dashboard button.
@login_required
def course_wizard_review(request, course_id):
    course, bail = _get_draft_course_or_redirect(request, course_id)
    if bail:
        return bail
    modules = course.modules.prefetch_related('lectures', 'quiz__questions').order_by('order', 'id')

    if request.method == 'POST':
        course.status = Course.Status.PENDING_REVIEW
        course.save()
        emails.send_course_submission_notification(course)
        messages.success(
            request,
            _('%(title)s submitted for review. We\'ll email you once it\'s approved.') % {'title': course.title})
        return redirect('instructor_dashboard')

    modules_summary = [{
        'module': module,
        'lecture': module.lectures.first(),
        'content_ready': _module_content_ready(course, module),
        'question_count': module.quiz.questions.count() if getattr(module, 'quiz', None) else 0,
    } for module in modules]

    return render(request, 'dashboard/wizard_review.html', {
        'course': course, 'modules_summary': modules_summary,
        'all_ready': all(m['content_ready'] for m in modules_summary) if modules_summary else False,
        'wizard_step_choices': WIZARD_STEP_CHOICES, 'current_step': 4,
    })


def _with_stats(queryset):
    """Annotate courses with a live average rating and enrolled-student count,
    for display on course cards and detail pages. Also prefetches
    modules/lectures so Course.total_duration_seconds()/
    effective_thumbnail_url() (card-grid duration + auto-thumbnail
    fallback) stay a single query instead of one extra query per course."""
    return queryset.annotate(
        avg_rating=Avg('reviews__rating'), enrolled_count=Count('enrollments')
    ).prefetch_related('modules__lectures')


def _rating_breakdown(course):
    """Per-star (5..1) review count and percentage of this course's total
    reviews, for the lesson player Overview tab's ratings breakdown bars --
    Review only stores each individual rating, so this aggregation doesn't
    exist anywhere else yet."""
    total = course.reviews.count()
    counts = {row['rating']: row['n'] for row in course.reviews.values('rating').annotate(n=Count('id'))}
    return [
        {'stars': stars, 'count': counts.get(stars, 0),
         'percent': round(counts.get(stars, 0) * 100 / total) if total else 0}
        for stars in (5, 4, 3, 2, 1)
    ]


def _next_lecture_for_enrollment(enrollment):
    """The lecture 'Continue Learning'/'View Course' should jump straight
    into: the first lecture (in module/lecture order) this enrollment
    hasn't completed yet, or the very first lecture if nothing's been
    completed, or if the course has no lectures at all."""
    lectures = list(Lecture.objects.filter(module__course=enrollment.course)
                     .order_by('module__order', 'order'))
    if not lectures:
        return None
    completed_ids = set(
        enrollment.lecture_progress.filter(completed=True).values_list('lecture_id', flat=True))
    for lecture in lectures:
        if lecture.id not in completed_ids:
            return lecture
    return lectures[0]  # every lecture done -- reopen from the start rather than dead-end


def _can_preview_unpublished(user, course):
    """The owning instructor and admins can always see their own course
    regardless of status (draft, pending review, archived, ...) -- everyone
    else only when it's actually published."""
    return user.is_authenticated and (course.instructor_id == user.id or user.is_superuser)


def _reenter_review_if_published(request, course):
    """Editing a live course's content must not silently change what
    students already see -- it goes back through admin review instead.
    Already-enrolled students keep access regardless (see course_player/
    course_detail, which never gate on status for them), so nothing they
    already paid for disappears while the edit is pending."""
    if course.status == Course.Status.PUBLISHED:
        course.status = Course.Status.PENDING_REVIEW
        course.rejection_reason = ''
        course.save()
        # This is the same "needs admin review" event course_wizard_review/
        # toggle_publish send on first submission -- called from here too
        # (module/lecture/quiz/question/choice edits on a live course, via
        # every caller of this helper) so a resubmission notifies admins
        # the same way a first submission does. The pending_courses_count
        # badge already picked this up for free (it's a live DB count), but
        # nothing previously sent the actual email for this specific path.
        emails.send_course_submission_notification(course)
        messages.info(request, _('%(title)s was live, so this change has been resubmitted for admin review.') % {'title': course.title})


# 6. Course Catalog - Browse all published courses
def course_catalog(request):
    courses = _with_stats(
        Course.objects.filter(status=Course.Status.PUBLISHED)).order_by('-created_at')
    return render(request, 'courses/catalog.html', {'courses': courses})

# 7. Course Detail - View a single course + its curriculum
def course_detail(request, course_id):
    course = get_object_or_404(_with_stats(Course.objects.all()), id=course_id)
    modules = course.modules.prefetch_related('lectures', 'quiz__questions__choices').order_by('order')
    reviews = course.reviews.select_related('student').order_by('-created_at')

    enrollment = None
    user_review = None
    if request.user.is_authenticated:
        enrollment = get_or_create_enrollment(request.user, course)
        user_review = reviews.filter(student=request.user).first()

    # The owning instructor and admins can always see every lecture/quiz in
    # full, regardless of the is_preview flag or enrollment -- same rule
    # course_player() already enforces server-side for actually watching a
    # video. Without this, an admin reviewing a course in the Course
    # Approval Queue could see titles but not open most of the actual
    # video/quiz content, since instructors mark only a couple of sample
    # lectures as "free preview".
    can_preview_all = _can_preview_unpublished(request.user, course)

    # Not published: only visible to someone who already has a reason to see
    # it (enrolled, the owning instructor, or an admin) -- not the public.
    if course.status != Course.Status.PUBLISHED and enrollment is None and not can_preview_all:
        raise Http404

    can_review = bool(
        request.user.is_authenticated and request.user.is_student
        and enrollment is not None and user_review is None
    )

    return render(request, 'courses/detail.html', {
        'course': course,
        'modules': modules,
        'reviews': reviews,
        'enrollment': enrollment,
        'can_preview_all': can_preview_all,
        'can_review': can_review,
        'review_form': ReviewForm() if can_review else None,
    })


# Enroll in a free course instantly. Paid courses go through checkout_course instead.
@login_required
def enroll_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, status=Course.Status.PUBLISHED)
    if not request.user.is_student:
        return redirect('course_detail', course_id=course.id)

    if Enrollment.objects.filter(student=request.user, course=course).exists():
        return redirect('course_detail', course_id=course.id)

    if course.is_free or course.price == 0:
        enrollment = Enrollment.objects.create(student=request.user, course=course)
        emails.send_enrollment_confirmation_email(enrollment)
        messages.success(request, _('You are now enrolled in %(title)s.') % {'title': course.title})
        return redirect('my_learning')

    if student_has_access(request.user, course):
        # Active subscriber -- no checkout needed, just unlock the course.
        enrollment = get_or_create_enrollment(request.user, course)
        emails.send_enrollment_confirmation_email(enrollment)
        messages.success(request, _('You are now enrolled in %(title)s.') % {'title': course.title})
        return redirect('my_learning')

    return redirect('checkout_course', course_id=course.id)


def _paymob_billing_data(user):
    return {
        'first_name': user.first_name or user.username,
        'last_name': user.last_name or 'Student',
        'email': user.email or f'{user.username}@example.com',
        'phone_number': user.phone_number or 'NA',
        'country': 'EG', 'city': 'NA', 'state': 'NA',
        'street': 'NA', 'building': 'NA', 'floor': 'NA', 'apartment': 'NA',
    }


# Start a Paymob checkout for a paid course. This only redirects the student
# to Paymob's hosted iframe -- it must NOT create the Payment/Enrollment, since
# a browser reaching this view is not proof of payment. That happens in the
# webhook once Paymob confirms the transaction succeeded.
@login_required
def checkout_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, status=Course.Status.PUBLISHED)
    if not request.user.is_student or course.is_free or course.price == 0:
        return redirect('course_detail', course_id=course.id)
    if Enrollment.objects.filter(student=request.user, course=course).exists():
        return redirect('course_detail', course_id=course.id)

    plans = Plan.objects.filter(is_active=True)

    if request.method == 'POST':
        option = request.POST.get('option')
        plan = plans.filter(id=request.POST.get('plan_id')).first() if option == 'subscription' else None

        if plan:
            merchant_order_id = f'sub{plan.id}-student{request.user.id}-{uuid.uuid4().hex[:10]}'
            amount_cents = int(plan.price_egp * 100)
        else:
            merchant_order_id = f'course{course.id}-student{request.user.id}-{uuid.uuid4().hex[:10]}'
            amount_cents = int(course.price * 100)

        try:
            checkout_url = paymob.initiate_checkout(
                amount_cents, merchant_order_id, _paymob_billing_data(request.user))
        except requests.RequestException:
            messages.error(request, _('Unable to start checkout right now. Please try again shortly.'))
            return redirect('course_detail', course_id=course.id)

        return redirect(checkout_url)

    return render(request, 'courses/checkout.html', {'course': course, 'plans': plans})


# Bunny Stream encoding webhook -- flips a lecture's bunny_status as the video
# moves through Bunny's pipeline (uploaded -> transcoding -> finished). Only
# ever updates a status int matched by GUID, so an unauthenticated caller can
# do nothing worse than nudge a status; no money or access decision rests on
# it. Bunny payload: {"VideoLibraryId": ..., "VideoGuid": "...", "Status": 4}.
@csrf_exempt
def bunny_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        payload = json.loads(request.body)
        guid = payload.get('VideoGuid', '')
        status = int(payload.get('Status', 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.error('[BUNNY_STATUS_DEBUG] bunny_webhook got an unparseable body: %r', request.body[:500])
        return HttpResponse(status=400)

    # TEMPORARY: [BUNNY_STATUS_DEBUG] -- diagnosing lectures stuck on
    # "still processing" long after Bunny actually finished. This is the only
    # thing that updates bunny_status, and Bunny's webhook delivery isn't
    # guaranteed, so logging every delivery we do get shows whether this view
    # is even being called.
    logger.info('[BUNNY_STATUS_DEBUG] bunny_webhook received: guid=%s status=%s', guid, status)
    if guid:
        updated = Lecture.objects.filter(bunny_video_id=guid).update(bunny_status=status)
        logger.info('[BUNNY_STATUS_DEBUG] bunny_webhook updated %d lecture(s) for guid=%s to status=%s',
                    updated, guid, status)
    return HttpResponse(status=200)


# Paymob webhook -- this is what actually creates the Payment + Enrollment +
# wallet credit. Idempotent: the unique constraint on provider_transaction_id
# means a retried webhook can't double-credit a wallet.
@csrf_exempt
def paymob_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)

    obj = payload.get('obj', {})
    received_hmac = request.GET.get('hmac', '')
    if not paymob.verify_hmac(paymob.flatten_callback_obj(obj), received_hmac):
        return HttpResponse(status=403)

    if not obj.get('success'):
        return HttpResponse(status=200)  # nothing to do for a failed/pending transaction

    transaction_id = str(obj.get('id'))
    order = obj.get('order') or {}
    merchant_order_id = order.get('merchant_order_id', '') if isinstance(order, dict) else ''

    course_match = re.match(r'course(\d+)-student(\d+)-', merchant_order_id)
    sub_match = re.match(r'sub(\d+)-student(\d+)-', merchant_order_id)

    if obj.get('is_refunded'):
        if sub_match:
            _process_subscription_refund(transaction_id)
        else:
            _process_refund(transaction_id)
        return HttpResponse(status=200)

    try:
        if course_match:
            _handle_course_payment(transaction_id, obj, int(course_match.group(1)), int(course_match.group(2)))
        elif sub_match:
            _handle_subscription_payment(transaction_id, obj, int(sub_match.group(1)), int(sub_match.group(2)))
        else:
            return HttpResponse(status=400)
    except IntegrityError:
        pass  # duplicate webhook delivery for a transaction we've already processed

    return HttpResponse(status=200)


def _handle_course_payment(transaction_id, obj, course_id, student_id):
    with transaction.atomic():
        course = Course.objects.select_related('instructor').get(id=course_id)
        student = User.objects.get(id=student_id)
        amount = Decimal(str(obj.get('amount_cents', 0))) / Decimal('100')

        payment, created = Payment.objects.get_or_create(
            provider_transaction_id=transaction_id,
            defaults={
                'student': student, 'course': course, 'total_amount': amount,
                'status': Payment.Status.SUCCEEDED,
            },
        )
        if created:
            wallet, _created = InstructorWallet.objects.get_or_create(instructor=course.instructor)
            wallet = InstructorWallet.objects.select_for_update().get(pk=wallet.pk)
            wallet.available_balance += payment.instructor_amount
            wallet.total_earnings += payment.instructor_amount
            wallet.save()
            WalletTransaction.objects.create(
                wallet=wallet, type=WalletTransaction.Type.SALE_CREDIT,
                amount=payment.instructor_amount, balance_after=wallet.available_balance,
                payment=payment)
            enrollment, _created = Enrollment.objects.get_or_create(
                student=student, course=course, defaults={'payment': payment})
            emails.send_course_purchase_notification(payment)
            emails.send_enrollment_confirmation_email(enrollment)


def _handle_subscription_payment(transaction_id, obj, plan_id, student_id):
    with transaction.atomic():
        plan = Plan.objects.get(id=plan_id)
        student = User.objects.get(id=student_id)
        amount = Decimal(str(obj.get('amount_cents', 0))) / Decimal('100')
        now = timezone.now()

        subscription, created = Subscription.objects.get_or_create(
            provider_transaction_id=transaction_id,
            defaults={
                'student': student, 'plan': plan, 'amount_paid': amount,
                'currency': 'EGP', 'expires_at': now + timedelta(days=plan.duration_days),
            },
        )
        if created:
            # One period spanning the whole subscription term -- distribution
            # (and instructor payout) happens once the period ends, not
            # re-sliced monthly even for the annual plan. See
            # SubscriptionPeriod's docstring for why.
            SubscriptionPeriod.objects.create(
                subscription=subscription, period_start=subscription.started_at,
                period_end=subscription.expires_at, amount_paid=subscription.amount_paid,
                currency=subscription.currency,
            )
            emails.send_subscription_notification(subscription)


def _process_refund(transaction_id):
    payment = Payment.objects.filter(
        provider_transaction_id=transaction_id, status=Payment.Status.SUCCEEDED).first()
    if payment is None:
        return
    payment.status = Payment.Status.REFUNDED
    payment.save()

    wallet = InstructorWallet.objects.select_for_update().get(instructor=payment.course.instructor)
    wallet.available_balance -= payment.instructor_amount
    wallet.save()
    WalletTransaction.objects.create(
        wallet=wallet, type=WalletTransaction.Type.REFUND_DEBIT,
        amount=payment.instructor_amount, balance_after=wallet.available_balance,
        payment=payment, note=f'Refund for transaction {transaction_id}')


def _process_subscription_refund(transaction_id):
    subscription = Subscription.objects.filter(provider_transaction_id=transaction_id).first()
    if subscription is None:
        return
    subscription.status = Subscription.Status.CANCELED
    subscription.save()

    period = SubscriptionPeriod.objects.filter(subscription=subscription).first()
    if period is None:
        return

    if period.status != SubscriptionPeriod.Status.DISTRIBUTED:
        # Nothing paid out yet -- just close the period so the distribution
        # job skips it.
        period.status = SubscriptionPeriod.Status.CANCELED
        period.save()
        return

    # Already distributed: reverse each instructor's credit individually.
    # Never edit the original RevenueDistribution/WalletTransaction rows --
    # the ledger is append-only, so a refund is its own new entry.
    for dist in RevenueDistribution.objects.filter(period=period).select_related('instructor'):
        wallet = InstructorWallet.objects.select_for_update().get(instructor=dist.instructor)
        wallet.available_balance -= dist.instructor_amount
        wallet.save()
        WalletTransaction.objects.create(
            wallet=wallet, type=WalletTransaction.Type.REFUND_DEBIT,
            amount=dist.instructor_amount, balance_after=wallet.available_balance,
            note=f'Refund for subscription {subscription.id}, course "{dist.course.title}"')


# Leave a review -- enrolled students only, one review per student per course.
@login_required
def add_review(request, course_id):
    course = get_object_or_404(Course, id=course_id, status=Course.Status.PUBLISHED)
    is_enrolled = Enrollment.objects.filter(student=request.user, course=course).exists()
    already_reviewed = Review.objects.filter(student=request.user, course=course).exists()

    if request.method == 'POST' and is_enrolled and not already_reviewed:
        form = ReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.student = request.user
            review.course = course
            review.save()

    return redirect('course_detail', course_id=course.id)


# My Learning - enrolled courses with progress
@login_required
def my_learning(request):
    enrollments = (Enrollment.objects.filter(student=request.user)
                   .select_related('course').order_by('-enrolled_at'))
    # "View Course" jumps straight into the player at the next incomplete
    # lecture for in-progress courses -- no next lecture (nothing left, or
    # no lectures at all) falls back to the course detail page instead of
    # a broken link. Computed once here rather than as a template method
    # call per row, since it needs a query per enrollment either way.
    next_lecture_by_enrollment = {
        enrollment.id: _next_lecture_for_enrollment(enrollment)
        for enrollment in enrollments if not enrollment.is_complete()
    }
    return render(request, 'courses/my_learning.html', {
        'enrollments': enrollments,
        'next_lecture_by_enrollment': next_lecture_by_enrollment,
    })


# "Continue Learning" on the homepage -- for a student with at least one
# enrollment, jumps directly into the player at their next incomplete
# lecture instead of landing on My Learning's list or a course's marketing
# detail page. Picks up where they actually left off: the course they most
# recently logged watch-time on, falling back to the most recently enrolled
# course if they haven't started watching anything yet.
@login_required
def continue_learning(request):
    if not request.user.is_student:
        return redirect('platform_home')

    last_watch = WatchEvent.objects.filter(student=request.user).select_related('course').first()
    if last_watch:
        enrollment = Enrollment.objects.filter(student=request.user, course=last_watch.course).first()
    else:
        enrollment = None
    if enrollment is None:
        enrollment = (Enrollment.objects.filter(student=request.user)
                      .select_related('course').order_by('-enrolled_at').first())

    if enrollment is None:
        return redirect('track_list')

    lecture = _next_lecture_for_enrollment(enrollment)
    if lecture is None:
        return redirect('course_detail', course_id=enrollment.course_id)

    return redirect('course_player', course_id=enrollment.course_id, lecture_id=lecture.id)


# Course Player - watch a lecture. Preview lectures are open to anyone;
# everything else requires an active enrollment.
def course_player(request, course_id, lecture_id):
    course = get_object_or_404(_with_stats(Course.objects.all()), id=course_id)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course=course)

    # Lazily backfills the poster for any course that predates this field
    # (or was created outside the normal form, e.g. via Django admin) --
    # same "generate on first access, cache the file" pattern as
    # certificate_download's certificate.generate_pdf() fallback.
    if not course.poster_image:
        _generate_poster_safely(course)

    has_access = request.user.is_authenticated and student_has_access(request.user, course)
    is_owner_or_admin = _can_preview_unpublished(request.user, course)

    # Not published: same rule as course_detail -- invisible to the public,
    # regardless of preview flag, unless already enrolled or the owner/admin.
    if course.status != Course.Status.PUBLISHED and not has_access and not is_owner_or_admin:
        raise Http404

    if not has_access and not is_owner_or_admin and not lecture.is_preview:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return HttpResponseForbidden(_('Enroll in this course to watch this lecture.'))

    enrollment = get_or_create_enrollment(request.user, course) if has_access else None

    modules = course.modules.prefetch_related('lectures', 'quiz__questions').order_by('order')
    all_lectures = list(Lecture.objects.filter(module__course=course).order_by('module__order', 'order'))

    # Every lecture in the course, not just the one being watched -- the
    # Overview tab's total-duration sum needs every lecture's
    # duration_seconds backfilled, and a lecture nobody's individually
    # opened (in the player or the classic editor) since upload would
    # otherwise never get synced. Each call is a fast no-op once a lecture
    # already has both a ready status and a known duration (see the guard
    # in _sync_bunny_status), so this only actually costs Bunny API calls
    # for lectures still missing data -- it doesn't retry forever once
    # everything is backfilled.
    for l in all_lectures:
        _sync_bunny_status(l)

    index = next((i for i, l in enumerate(all_lectures) if l.id == lecture.id), 0)
    prev_lecture = all_lectures[index - 1] if index > 0 else None
    next_lecture = all_lectures[index + 1] if index < len(all_lectures) - 1 else None

    progress = None
    completed_lecture_ids = set()
    passed_quiz_ids = set()
    if enrollment is not None:
        progress = LectureProgress.objects.filter(enrollment=enrollment, lecture=lecture).first()
        completed_lecture_ids = set(
            LectureProgress.objects.filter(enrollment=enrollment, completed=True)
            .values_list('lecture_id', flat=True))
        passed_quiz_ids = set(
            enrollment.quiz_attempts.filter(passed=True).values_list('quiz_id', flat=True))

    # Signed here (not in the template) because the token is time-limited and
    # uses the secret key -- a fresh, expiring URL is minted on every load.
    bunny_embed_url = bunny.embed_url(lecture.bunny_video_id) if lecture.bunny_video_id else None

    # Overview tab: total course duration is the sum across every lecture in
    # every module (not just the current one), since a module can hold
    # several shorter lessons.
    total_duration_seconds = sum(l.duration_seconds for l in all_lectures)

    # ai_generated_script is populated by two independent paths -- a Script
    # Only course's manual script, or the "Generate Transcript" button
    # (any production type, once an instructor runs it) -- so the tab
    # shows whenever there's actually text in it, regardless of
    # production_type.
    has_transcript = bool(lecture.ai_generated_script and lecture.ai_generated_script.strip())

    return render(request, 'courses/player.html', {
        'course': course,
        'lecture': lecture,
        'bunny_embed_url': bunny_embed_url,
        'modules': modules,
        'enrollment': enrollment,
        'progress': progress,
        'prev_lecture': prev_lecture,
        'next_lecture': next_lecture,
        'completed_lecture_ids': completed_lecture_ids,
        'passed_quiz_ids': passed_quiz_ids,
        'total_duration_seconds': total_duration_seconds,
        'has_transcript': has_transcript,
        'rating_breakdown': _rating_breakdown(course),
    })


# Mark a lecture complete for the current student's enrollment
@login_required
def mark_lecture_complete(request, course_id, lecture_id):
    course = get_object_or_404(Course, id=course_id)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course=course)
    enrollment = get_object_or_404(Enrollment, student=request.user, course=course)

    if request.method == 'POST':
        progress, _created = LectureProgress.objects.get_or_create(enrollment=enrollment, lecture=lecture)
        progress.completed = True
        progress.completed_at = timezone.now()
        progress.save()
        enrollment.issue_certificate_if_complete()

    return redirect('course_player', course_id=course.id, lecture_id=lecture.id)


# A Module's optional Quiz: GET renders the question form, POST grades it,
# records the attempt, and (if this was the last gate) issues the
# certificate. Enrollment existing is the access check, same convention as
# mark_lecture_complete above.
@login_required
def take_quiz(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id)
    module = get_object_or_404(Module, id=module_id, course=course)
    quiz = get_object_or_404(Quiz, module=module)
    enrollment = get_object_or_404(Enrollment, student=request.user, course=course)

    questions = list(quiz.questions.prefetch_related('choices'))
    if not questions:
        raise Http404('This quiz has no questions yet.')

    if request.method == 'POST':
        correct_count = 0
        pending_answers = []
        for question in questions:
            submitted_choice_id = request.POST.get(f'question_{question.id}')
            selected_choice = None
            is_correct = False
            if submitted_choice_id:
                selected_choice = next(
                    (c for c in question.choices.all() if str(c.id) == submitted_choice_id), None)
                is_correct = bool(selected_choice and selected_choice.is_correct)
            if is_correct:
                correct_count += 1
            pending_answers.append((question, selected_choice, is_correct))

        score_percent = (Decimal(correct_count * 100) / Decimal(len(questions))).quantize(Decimal('0.01'))
        attempt = QuizAttempt.objects.create(
            enrollment=enrollment, quiz=quiz, score_percent=score_percent,
            passed=score_percent >= quiz.passing_score_percent,
        )
        QuizAnswer.objects.bulk_create([
            QuizAnswer(attempt=attempt, question=q, selected_choice=sc, is_correct=ic)
            for q, sc, ic in pending_answers
        ])

        if attempt.passed:
            enrollment.issue_certificate_if_complete()

        return redirect('quiz_result', course_id=course.id, module_id=module.id, attempt_id=attempt.id)

    best_attempt = enrollment.quiz_attempts.filter(quiz=quiz).order_by('-score_percent').first()
    return render(request, 'courses/quiz.html', {
        'course': course, 'module': module, 'quiz': quiz, 'questions': questions,
        'best_attempt': best_attempt,
    })


@login_required
def quiz_result(request, course_id, module_id, attempt_id):
    attempt = get_object_or_404(
        QuizAttempt, id=attempt_id, enrollment__student=request.user,
        enrollment__course_id=course_id, quiz__module_id=module_id)
    return render(request, 'courses/quiz_result.html', {
        'course': attempt.enrollment.course, 'module': attempt.quiz.module, 'attempt': attempt,
    })


# A lecture counts as "watched" once accumulated watch-time reaches this
# fraction of its known duration -- matches the ~90% threshold requested for
# gating auto-complete, mirroring how most video platforms treat a lecture
# as finished without requiring every last second.
WATCH_COMPLETE_THRESHOLD = 0.9


# Records a client-flushed watch-time heartbeat (aggregated client-side,
# sent roughly every 30s -- never one row per second). This is the only
# input the subscription revenue-distribution job trusts; every check here
# exists because watch-time is now money and a browser client cannot be
# trusted to report it honestly. Also the trigger for watch-threshold-gated
# auto-complete: once this student's total logged watch-time on this lecture
# reaches WATCH_COMPLETE_THRESHOLD of its duration, it's marked complete here
# automatically, without waiting for the manual "Mark as Complete" click.
@login_required
def record_watch_event(request, course_id, lecture_id):
    if request.method != 'POST':
        return HttpResponse(status=405)

    course = get_object_or_404(Course, id=course_id)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course=course)

    if not student_has_access(request.user, course):
        return HttpResponseForbidden()

    try:
        seconds = int(json.loads(request.body).get('seconds', 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return HttpResponse(status=400)

    if seconds <= 0:
        return HttpResponse(status=400)

    # A single flush can't legitimately report more than the lecture's own
    # runtime plus slack for pause/seek jitter.
    if lecture.duration_seconds and seconds > lecture.duration_seconds * 1.5:
        return HttpResponse(status=400)

    last_event = WatchEvent.objects.filter(student=request.user).order_by('-occurred_at').first()
    if last_event:
        elapsed = (timezone.now() - last_event.occurred_at).total_seconds()
        # Reject a duration longer than real wall-clock time has actually
        # passed since the last heartbeat -- the strongest defense against a
        # spoofed client claiming impossible watch-time.
        if seconds > elapsed + 5:
            return HttpResponse(status=400)
        # Basic rate limit: a legitimate ~30s flush cadence can't arrive
        # faster than this.
        if elapsed < 10:
            return HttpResponse(status=429)

    WatchEvent.objects.create(student=request.user, lecture=lecture, course=course, seconds_watched=seconds)

    completed = False
    enrollment = Enrollment.objects.filter(student=request.user, course=course).first()
    if enrollment is not None and lecture.duration_seconds:
        progress = LectureProgress.objects.filter(enrollment=enrollment, lecture=lecture).first()
        if not progress or not progress.completed:
            total_watched = WatchEvent.objects.filter(
                student=request.user, lecture=lecture
            ).aggregate(total=Sum('seconds_watched'))['total'] or 0
            if total_watched >= lecture.duration_seconds * WATCH_COMPLETE_THRESHOLD:
                progress, _created = LectureProgress.objects.get_or_create(
                    enrollment=enrollment, lecture=lecture)
                progress.completed = True
                progress.completed_at = timezone.now()
                progress.save()
                enrollment.issue_certificate_if_complete()
                completed = True
        elif progress.completed:
            completed = True

    return JsonResponse({'completed': completed})


# Public certificate verification page -- no login required, so anyone
# (e.g. an employer following a LinkedIn link) can confirm authenticity.
def certificate_view(request, certificate_uuid):
    certificate = get_object_or_404(Certificate, uuid=certificate_uuid)
    return render(request, 'courses/certificate.html', {
        'certificate': certificate,
        'verification_url': certificates.verification_url(certificate),
        'linkedin_share_url': certificates.linkedin_share_url(certificate),
    })


def certificate_download(request, certificate_uuid):
    certificate = get_object_or_404(Certificate, uuid=certificate_uuid)
    if not certificate.pdf_file:
        certificate.generate_pdf()
    response = HttpResponse(certificate.pdf_file.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="certificate-{certificate.uuid}.pdf"'
    return response


# Student uploads/edits their homework for a lecture that accepts one.
# Enrollment is required (same gate as mark_lecture_complete) -- a preview
# viewer isn't a real student and shouldn't be able to submit graded work.
@login_required
def submit_homework(request, course_id, lecture_id):
    course = get_object_or_404(Course, id=course_id)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course=course, accepts_submission=True)
    enrollment = get_object_or_404(Enrollment, student=request.user, course=course)

    submission = Submission.objects.filter(student=request.user, lecture=lecture).first()
    if submission and submission.is_graded:
        return render(request, 'courses/submit_homework.html', {
            'course': course, 'lecture': lecture, 'submission': submission, 'form': None,
        })

    if request.method == 'POST':
        form = SubmissionForm(request.POST, request.FILES, instance=submission)
        if form.is_valid():
            new_submission = form.save(commit=False)
            new_submission.student = request.user
            new_submission.lecture = lecture
            new_submission.save()
            messages.success(request, _('Homework submitted.'))
            return redirect('course_player', course_id=course.id, lecture_id=lecture.id)
    else:
        form = SubmissionForm(instance=submission)

    return render(request, 'courses/submit_homework.html', {
        'course': course, 'lecture': lecture, 'submission': submission, 'form': form,
    })


# AI Study Buddy -- a persistent chat with Mendoura AI Coach. Each student has
# one running conversation that this view always resumes (no thread picker);
# the assistant's markdown replies are rendered to HTML server-side since the
# frontend has no CDN-hosted markdown library to reach for.
@login_required
def ai_coach(request):
    if not request.user.is_student:
        return redirect('platform_home')

    conversation = AIConversation.objects.filter(student=request.user).first()
    if conversation is None:
        conversation = AIConversation.objects.create(student=request.user)

    messages_out = [
        {'role': m.role, 'content': m.content,
         'html': markdown.markdown(m.content, extensions=['fenced_code', 'tables', 'nl2br'])
                 if m.role == AIMessage.Role.ASSISTANT else None}
        for m in conversation.messages.all()
    ]
    return render(request, 'dashboard/ai_buddy.html', {
        'conversation': conversation,
        'chat_messages': messages_out,
        'ai_configured': ai_coach_client.is_configured(),
    })


# JSON endpoint the chat page POSTs a new student message to. Stores both
# sides of the exchange and returns the assistant's reply as ready-to-insert
# HTML (already markdown-rendered) plus the plain text for history replay.
@login_required
def ai_coach_send(request):
    if not request.user.is_student:
        return HttpResponseForbidden()
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        text = json.loads(request.body).get('message', '').strip()
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({'error': _('Malformed request.')}, status=400)

    if not text:
        return JsonResponse({'error': _('Message cannot be empty.')}, status=400)
    if len(text) > 6000:
        return JsonResponse({'error': _('That message is too long -- please shorten it.')}, status=400)

    conversation = AIConversation.objects.filter(student=request.user).first()
    if conversation is None:
        conversation = AIConversation.objects.create(student=request.user)

    AIMessage.objects.create(conversation=conversation, role=AIMessage.Role.USER, content=text)

    # Bound how much history we replay to the API -- a long-running study
    # thread shouldn't grow the request payload (and cost) without limit.
    history = [
        {'role': m.role, 'content': m.content}
        for m in conversation.messages.order_by('-created_at')[:40]
    ][::-1]

    try:
        reply = ai_coach_client.send_message(history, user_id=request.user.id)
    except ai_coach_client.AICoachError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    AIMessage.objects.create(conversation=conversation, role=AIMessage.Role.ASSISTANT, content=reply)
    conversation.save(update_fields=['updated_at'])

    return JsonResponse({
        'reply_html': markdown.markdown(reply, extensions=['fenced_code', 'tables', 'nl2br']),
    })


# Lets the floating AI Coach widget (base.html, any page) preload the same
# persisted conversation the full /dashboard/ai-coach/ page shows, without
# a full page render -- opening the widget looks like resuming one ongoing
# thread instead of starting over.
@login_required
def ai_coach_widget_history(request):
    if not request.user.is_student:
        return HttpResponseForbidden()

    conversation = AIConversation.objects.filter(student=request.user).first()
    messages_out = [
        {'role': m.role,
         'html': markdown.markdown(m.content, extensions=['fenced_code', 'tables', 'nl2br'])
                 if m.role == AIMessage.Role.ASSISTANT else None,
         'content': m.content}
        for m in (conversation.messages.all() if conversation else [])
    ]
    return JsonResponse({'messages': messages_out})


def _lesson_ai_context(course, lecture):
    """Grounding text for the lesson-embedded AI Coach: everything actually
    known about the current lesson's module, so it can answer "brief this
    lesson"/"summarize this module" from real text. ai_generated_script is
    populated by two independent paths -- a Script Only course's manual
    script, or the "Generate Transcript" button (any production type) --
    so a lesson counts as having a transcript whenever that field actually
    has text, not based on production_type. Lessons without one are
    labeled as such rather than silently omitted, so the model can tell
    the student which lessons it couldn't cover instead of pretending the
    gap doesn't exist."""
    module = lecture.module
    lines = [
        f'Course: {course.title}',
        f"Course description: {(course.description or '').strip()[:1500]}",
        f'Module: {module.title}',
        f'Lesson the student is currently viewing: {lecture.title}',
        '',
        f"All lessons in the '{module.title}' module, in order:",
    ]
    for l in module.lectures.order_by('order', 'id'):
        marker = ' <- currently viewing' if l.id == lecture.id else ''
        lines.append(f'- "{l.title}"{marker}')
        if l.ai_generated_script and l.ai_generated_script.strip():
            lines.append(f'  Transcript: """{l.ai_generated_script.strip()[:4000]}"""')
        else:
            lines.append('  Transcript: not available for this lesson.')
    return '\n'.join(lines)


# Ephemeral counterpart to ai_coach_send for the lesson-embedded AI Coach
# widget on the player page: grounded in the current lesson/module's actual
# content (see _lesson_ai_context) instead of general chat. Deliberately
# does not persist to AIConversation/AIMessage -- this is a short per-lesson
# Q&A, not the student's one long-running study thread, so the client holds
# and resends its own short-lived history each turn.
@login_required
def ai_coach_lesson_send(request, course_id, lecture_id):
    if not request.user.is_student:
        return HttpResponseForbidden()
    if request.method != 'POST':
        return HttpResponse(status=405)

    course = get_object_or_404(Course, id=course_id)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course=course)
    if not student_has_access(request.user, course) and not lecture.is_preview:
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body)
        text = (payload.get('message') or '').strip()
        prior_history = payload.get('history') or []
    except (ValueError, json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': _('Malformed request.')}, status=400)

    if not text:
        return JsonResponse({'error': _('Message cannot be empty.')}, status=400)
    if len(text) > 6000:
        return JsonResponse({'error': _('That message is too long -- please shorten it.')}, status=400)
    if not isinstance(prior_history, list):
        return JsonResponse({'error': _('Malformed request.')}, status=400)

    # Client-held history, same cap as the persisted thread -- trusted only
    # as far as role/content strings, never as anything executable.
    history = [
        {'role': m.get('role'), 'content': str(m.get('content', ''))[:6000]}
        for m in prior_history[-40:] if m.get('role') in ('user', 'assistant')
    ]
    history.append({'role': 'user', 'content': text})

    try:
        reply = ai_coach_client.send_message(
            history, user_id=request.user.id, context=_lesson_ai_context(course, lecture))
    except ai_coach_client.AICoachError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    return JsonResponse({
        'reply': reply,
        'reply_html': markdown.markdown(reply, extensions=['fenced_code', 'tables', 'nl2br']),
    })


# Browse top-level Track categories (Tech, Languages, Marketing, Business, Design, ...)
def track_list(request):
    tracks = Track.objects.filter(parent__isnull=True, is_active=True)
    return render(request, 'courses/track_list.html', {'tracks': tracks})


def _roadmap_for_student(track, user):
    """Build the ordered roadmap steps for a leaf track, each annotated with a
    state the template can key off of: 'complete', 'in_progress', 'locked',
    'available', or 'planned' (no course linked to this step yet)."""
    steps = list(track.roadmap_steps.select_related('course').order_by('order'))
    if not user.is_authenticated:
        return [{'step': s, 'state': 'planned' if not s.course else 'available'} for s in steps]

    enrollments = {
        e.course_id: e for e in
        Enrollment.objects.filter(student=user, course__in=[s.course_id for s in steps if s.course_id])
    }
    result = []
    unlocked = True
    for s in steps:
        if not s.course:
            result.append({'step': s, 'state': 'planned'})
            continue
        enrollment = enrollments.get(s.course_id)
        if enrollment and enrollment.is_complete():
            state = 'complete'
        elif enrollment:
            state = 'in_progress'
        elif unlocked:
            state = 'available'
        else:
            state = 'locked'
        result.append({'step': s, 'state': state, 'enrollment': enrollment})
        if not (enrollment and enrollment.is_complete()) and not s.is_optional:
            unlocked = False
    return result


# A Track's published courses (leaf track) or its child tracks (parent track)
def track_detail(request, slug):
    track = get_object_or_404(
        Track.objects.select_related('parent').prefetch_related('children'),
        slug=slug, is_active=True,
    )

    if track.children.exists():
        children = track.children.filter(is_active=True)
        return render(request, 'courses/track_detail.html', {
            'track': track, 'children': children, 'is_parent': True,
        })

    courses = _with_stats(Course.objects.filter(
        track=track, status=Course.Status.PUBLISHED)).order_by('-created_at')
    roadmap = _roadmap_for_student(track, request.user)
    return render(request, 'courses/track_detail.html', {
        'track': track, 'courses': courses, 'roadmap': roadmap,
    })


# Full-text search across Tracks and Courses
def search_results(request):
    query = request.GET.get('q', '').strip()
    level = request.GET.get('level', '')
    price = request.GET.get('price', '')
    language = request.GET.get('language', '')
    track_slug = request.GET.get('track', '')

    tracks = Track.objects.none()
    courses = Course.objects.none()

    if query:
        search_query = SearchQuery(query)

        tracks = (
            Track.objects.filter(is_active=True)
            .annotate(
                search=SearchVector('name', 'description'),
                rank=SearchRank(SearchVector('name', 'description'), search_query),
            )
            .filter(search=search_query)
            .order_by('-rank')
        )

        courses = _with_stats(Course.objects.filter(status=Course.Status.PUBLISHED)).annotate(
            search=SearchVector('title', 'subtitle', 'description'),
            rank=SearchRank(SearchVector('title', 'subtitle', 'description'), search_query),
        ).filter(search=search_query)

        if level:
            courses = courses.filter(level=level)
        if price == 'free':
            courses = courses.filter(is_free=True)
        elif price == 'paid':
            courses = courses.filter(is_free=False)
        if language:
            courses = courses.filter(language__iexact=language)
        if track_slug:
            courses = courses.filter(track__slug=track_slug)

        courses = courses.order_by('-rank')

    return render(request, 'courses/search_results.html', {
        'query': query,
        'tracks': tracks,
        'courses': courses,
        'selected_level': level,
        'selected_price': price,
        'selected_language': language,
        'selected_track': track_slug,
        'levels': Course.Level.choices,
        'all_tracks': Track.objects.filter(parent__isnull=False, is_active=True).order_by('name'),
    })

# 8. Submit a draft/rejected course for admin review (instructors cannot self-publish)
@login_required
def toggle_publish(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    if course.status in (Course.Status.DRAFT, Course.Status.REJECTED):
        course.status = Course.Status.PENDING_REVIEW
        course.save()
        emails.send_course_submission_notification(course)
    return redirect('instructor_dashboard')


# Edit an existing course's own details. If it was live, the edit resubmits
# it for review rather than silently changing what students already see.
@login_required
def edit_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    if request.method == 'POST':
        form = CourseCreationForm(request.POST, request.FILES, instance=course)
        # production_type is frozen once a course has its first successful
        # sale (Course.save() enforces this) -- disable the field so a
        # resubmitted, unchanged value can't trip that check.
        if course.has_successful_sale():
            form.fields['production_type'].disabled = True
        if form.is_valid():
            was_published = course.status == Course.Status.PUBLISHED
            # Regenerating the poster is cheap, but re-uploading it to
            # storage on every unrelated edit (price, description, ...)
            # isn't free and leaves the previous file orphaned -- only do
            # it when the source material for the poster actually changed.
            poster_inputs_changed = bool({'title', 'thumbnail'} & set(form.changed_data))
            form.save()
            if poster_inputs_changed or not course.poster_image:
                _generate_poster_safely(course)
            if was_published:
                _reenter_review_if_published(request, course)
            else:
                messages.success(request, _('%(title)s updated.') % {'title': course.title})
            return redirect('instructor_dashboard')
    else:
        form = CourseCreationForm(instance=course)
        if course.has_successful_sale():
            form.fields['production_type'].disabled = True
    return render(request, 'dashboard/edit_course.html', {'form': form, 'course': course})


# Archives instead of hard-deleting whenever the course has any history a
# student is relying on (an enrollment, a payment, watch-time, revenue
# distributions) -- on_delete=PROTECT on Payment/RevenueDistribution/
# WatchEvent backs this up at the DB level for anything this check misses.
@login_required
def delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    if request.method == 'POST':
        has_history = (
            Enrollment.objects.filter(course=course).exists()
            or Payment.objects.filter(course=course).exists()
        )
        if has_history:
            course.status = Course.Status.ARCHIVED
            course.save()
            messages.success(
                request,
                _('%(title)s has enrollment or payment history, so it has been archived '
                  'instead of deleted.') % {'title': course.title})
        else:
            try:
                course.delete()
                messages.success(request, _('%(title)s has been deleted.') % {'title': course.title})
            except ProtectedError:
                course.status = Course.Status.ARCHIVED
                course.save()
                messages.success(
                    request,
                    _('%(title)s has watch-time or revenue history, so it has been archived '
                      'instead of deleted.') % {'title': course.title})
    return redirect('instructor_dashboard')


# Manage a course's Modules (the sections of the curriculum)
@login_required
def manage_modules(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    modules = course.modules.order_by('order')

    if request.method == 'POST':
        form = ModuleForm(request.POST)
        if form.is_valid():
            module = form.save(commit=False)
            module.course = course
            module.save()
            _reenter_review_if_published(request, course)
            return redirect('manage_modules', course_id=course.id)
    else:
        form = ModuleForm()

    return render(request, 'dashboard/manage_modules.html', {
        'course': course, 'modules': modules, 'form': form,
    })


@login_required
def edit_module(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    if request.method == 'POST':
        form = ModuleForm(request.POST, instance=module)
        if form.is_valid():
            form.save()
            _reenter_review_if_published(request, course)
            return redirect('manage_modules', course_id=course.id)
    else:
        form = ModuleForm(instance=module)
    return render(request, 'dashboard/edit_module.html', {'course': course, 'module': module, 'form': form})


@login_required
def delete_module(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    if request.method == 'POST':
        try:
            module.delete()
            _reenter_review_if_published(request, course)
        except ProtectedError:
            messages.error(
                request,
                _('"%(title)s" has watch-time history on one of its lectures and cannot be '
                  'deleted.') % {'title': module.title})
    return redirect('manage_modules', course_id=course.id)


# Add lectures to a specific Module
@login_required
def manage_lectures(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    lectures = module.lectures.all()

    if request.method == 'POST':
        form = LectureForm(request.POST, request.FILES)
        if form.is_valid():
            lecture = form.save(commit=False)
            lecture.module = module
            lecture.save()
            _reenter_review_if_published(request, course)
            return redirect('manage_lectures', course_id=course.id, module_id=module.id)
    else:
        form = LectureForm()

    return render(request, 'dashboard/manage_lectures.html', {
        'course': course,
        'module': module,
        'lectures': lectures,
        'form': form,
        'resource_form': ResourceForm(),
    })


@login_required
def edit_lecture(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)
    course = lecture.module.course
    if request.method == 'GET':
        _sync_bunny_status(lecture)
    if request.method == 'POST':
        form = LectureForm(request.POST, request.FILES, instance=lecture)
        if form.is_valid():
            form.save()
            _reenter_review_if_published(request, course)
            return redirect('manage_lectures', course_id=course.id, module_id=lecture.module_id)
    else:
        form = LectureForm(instance=lecture)
    return render(request, 'dashboard/edit_lecture.html', {
        'course': course, 'module': lecture.module, 'lecture': lecture, 'form': form,
        'bunny_configured': bunny.is_configured(),
    })


def _run_transcript_generation(lecture_id):
    """Runs in a background thread kicked off by generate_lecture_transcript.
    This project has no task queue (Celery/etc.), so a plain daemon thread
    within the same process is the simplest way to avoid blocking the
    request for however long Gemini takes on a real lecture video --
    mirroring the same "best-effort, never block the page" philosophy as
    _sync_bunny_status elsewhere in this file. A dyno restart mid-run would
    leave the lecture stuck on "processing" with no automatic retry, same
    known limitation as a missed Bunny webhook; the instructor can just
    click the button again."""
    try:
        lecture = Lecture.objects.get(id=lecture_id)
    except Lecture.DoesNotExist:
        return
    try:
        text = ai_coach_client.transcribe_video(lecture.bunny_video_id)
    except ai_coach_client.AICoachError as exc:
        Lecture.objects.filter(id=lecture_id).update(
            transcript_status=Lecture.TranscriptStatus.FAILED, transcript_error=str(exc))
        return
    except Exception:
        logger.error('[TRANSCRIPT] unexpected failure generating transcript lecture_id=%s',
                      lecture_id, exc_info=True)
        Lecture.objects.filter(id=lecture_id).update(
            transcript_status=Lecture.TranscriptStatus.FAILED,
            transcript_error=_('An unexpected error occurred. Please try again.'))
        return
    Lecture.objects.filter(id=lecture_id).update(
        ai_generated_script=text, transcript_status=Lecture.TranscriptStatus.DONE, transcript_error='')


# Kicks off transcription in the background and returns immediately --
# edit_lecture.html polls lecture_transcript_status below to show progress
# and knows to reload once it's done/failed, rather than holding this
# request open for however long Gemini takes.
@login_required
def generate_lecture_transcript(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)
    if request.method != 'POST':
        return HttpResponse(status=405)

    if not lecture.bunny_video_id or not lecture.bunny_ready:
        messages.error(
            request,
            _('Upload a video and wait for it to finish processing before generating a transcript.'))
        return redirect('edit_lecture', lecture_id=lecture.id)

    # Idempotent -- clicking again while already processing doesn't start a
    # second thread racing the first to write the same row.
    if lecture.transcript_status != Lecture.TranscriptStatus.PROCESSING:
        lecture.transcript_status = Lecture.TranscriptStatus.PROCESSING
        lecture.transcript_error = ''
        lecture.save(update_fields=['transcript_status', 'transcript_error'])
        threading.Thread(target=_run_transcript_generation, args=(lecture.id,), daemon=True).start()

    return redirect('edit_lecture', lecture_id=lecture.id)


@login_required
def lecture_transcript_status(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)
    return JsonResponse({
        'status': lecture.transcript_status,
        'error': lecture.transcript_error,
        'has_script': bool(lecture.ai_generated_script and lecture.ai_generated_script.strip()),
    })


@login_required
def delete_lecture(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)
    course_id, module_id = lecture.module.course_id, lecture.module_id
    if request.method == 'POST':
        try:
            course = lecture.module.course
            lecture.delete()
            _reenter_review_if_published(request, course)
        except ProtectedError:
            messages.error(
                request, _('"%(title)s" has watch-time history and cannot be deleted.') % {'title': lecture.title})
    return redirect('manage_lectures', course_id=course_id, module_id=module_id)


# Manage a Module's optional Quiz: settings (title/passing score) + its
# list of Questions. A Module has at most one Quiz (OneToOneField), created
# lazily here the first time the instructor saves the settings form.
@login_required
def manage_quiz(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    quiz = getattr(module, 'quiz', None)

    if request.method == 'POST':
        form = QuizForm(request.POST, instance=quiz)
        if form.is_valid():
            quiz = form.save(commit=False)
            quiz.module = module
            quiz.save()
            _reenter_review_if_published(request, course)
            return redirect('manage_quiz', course_id=course.id, module_id=module.id)
    else:
        form = QuizForm(instance=quiz)

    questions = quiz.questions.prefetch_related('choices') if quiz else []

    return render(request, 'dashboard/manage_quiz.html', {
        'course': course, 'module': module, 'quiz': quiz, 'form': form,
        'question_form': QuestionForm(), 'questions': questions,
    })


@login_required
def delete_quiz(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    quiz = get_object_or_404(Quiz, module=module)
    if request.method == 'POST':
        quiz.delete()
        _reenter_review_if_published(request, course)
    return redirect('manage_modules', course_id=course.id)


@login_required
def add_question(request, course_id, module_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    module = get_object_or_404(Module, id=module_id, course=course)
    quiz = get_object_or_404(Quiz, module=module)
    if request.method == 'POST':
        form = QuestionForm(request.POST)
        if form.is_valid():
            question = form.save(commit=False)
            question.quiz = quiz
            question.save()
            _reenter_review_if_published(request, course)
            # Straight to the choices editor -- a question with no answer
            # choices can't be answered by a student, so that's always the
            # very next thing to do, not an optional extra step reached by
            # noticing the "Edit / Choices" link back on the quiz overview.
            return redirect('edit_question', question_id=question.id)
    return redirect('manage_quiz', course_id=course.id, module_id=module.id)


@login_required
def edit_question(request, question_id):
    question = get_object_or_404(Question, id=question_id, quiz__module__course__instructor=request.user)
    course = question.quiz.module.course
    if request.method == 'POST':
        form = QuestionForm(request.POST, instance=question)
        if form.is_valid():
            form.save()
            _reenter_review_if_published(request, course)
            return redirect('edit_question', question_id=question.id)
    else:
        form = QuestionForm(instance=question)
    return render(request, 'dashboard/edit_question.html', {
        'course': course, 'module': question.quiz.module, 'question': question, 'form': form,
        'choice_form': ChoiceForm(),
    })


@login_required
def delete_question(request, question_id):
    question = get_object_or_404(Question, id=question_id, quiz__module__course__instructor=request.user)
    course_id, module_id = question.quiz.module.course_id, question.quiz.module_id
    if request.method == 'POST':
        course = question.quiz.module.course
        question.delete()
        _reenter_review_if_published(request, course)
    return redirect('manage_quiz', course_id=course_id, module_id=module_id)


# Adding a choice that's marked correct un-marks every sibling choice first
# -- enforces "exactly one correct answer" for a single_choice question the
# same way a radio-button group would, without needing DB-level validation
# that would fight the incremental add-one-at-a-time UI.
@login_required
def add_choice(request, question_id):
    question = get_object_or_404(Question, id=question_id, quiz__module__course__instructor=request.user)
    course = question.quiz.module.course
    if request.method == 'POST':
        form = ChoiceForm(request.POST)
        if form.is_valid():
            choice = form.save(commit=False)
            choice.question = question
            if choice.is_correct:
                question.choices.update(is_correct=False)
            choice.save()
            _reenter_review_if_published(request, course)
    return redirect('edit_question', question_id=question.id)


@login_required
def mark_choice_correct(request, choice_id):
    choice = get_object_or_404(Choice, id=choice_id, question__quiz__module__course__instructor=request.user)
    question = choice.question
    if request.method == 'POST':
        question.choices.update(is_correct=False)
        choice.is_correct = True
        choice.save(update_fields=['is_correct'])
        _reenter_review_if_published(request, question.quiz.module.course)
    return redirect('edit_question', question_id=question.id)


@login_required
def delete_choice(request, choice_id):
    choice = get_object_or_404(Choice, id=choice_id, question__quiz__module__course__instructor=request.user)
    question_id = choice.question_id
    if request.method == 'POST':
        choice.delete()
    return redirect('edit_question', question_id=question_id)


# Creates the Bunny video record for a lecture and hands the browser a
# short-lived, single-video signature to upload straight to Bunny. The raw
# Bunny API key never leaves the server. Ownership-checked like every other
# instructor content action.
@login_required
def create_bunny_video(request, lecture_id):
    if request.method != 'POST':
        return HttpResponse(status=405)
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)

    if not bunny.is_configured():
        return JsonResponse({'error': _('Video hosting is not configured.')}, status=503)

    try:
        video_id = bunny.create_video(f'{lecture.course.title} - {lecture.title}')
    except (bunny.BunnyError, requests.RequestException):
        # bunny.create_video() already logs the request/response detail under
        # [BUNNY_UPLOAD_DEBUG] -- this adds the request-side context (which
        # lecture/course/instructor hit it) so a failure can be tied back to
        # a specific "Could not start the upload" report.
        logger.error(
            '[BUNNY_UPLOAD_DEBUG] create_bunny_video failed for lecture_id=%s course_id=%s instructor=%s',
            lecture.id, lecture.module.course_id, request.user.username, exc_info=True)
        return JsonResponse({'error': _('Could not start the upload. Please try again.')}, status=502)

    # Replacing an existing video: point the lecture at the new GUID. The old
    # Bunny video is orphaned rather than deleted -- harmless, and avoids a
    # second API call in the request path.
    lecture.bunny_video_id = video_id
    lecture.bunny_status = 0
    lecture.save(update_fields=['bunny_video_id', 'bunny_status'])

    _reenter_review_if_published(request, lecture.module.course)
    return JsonResponse(bunny.upload_credentials(video_id))


# Attach a downloadable Resource to a lecture
@login_required
def add_resource(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, module__course__instructor=request.user)
    if request.method == 'POST':
        form = ResourceForm(request.POST, request.FILES)
        if form.is_valid():
            resource = form.save(commit=False)
            resource.lecture = lecture
            if resource.file:
                resource.file_size = resource.file.size
            resource.save()
            _reenter_review_if_published(request, lecture.module.course)
    return redirect('manage_lectures', course_id=lecture.module.course_id, module_id=lecture.module_id)


@login_required
def delete_resource(request, resource_id):
    resource = get_object_or_404(Resource, id=resource_id, lecture__module__course__instructor=request.user)
    course_id, module_id = resource.lecture.module.course_id, resource.lecture.module_id
    if request.method == 'POST':
        resource.delete()
    return redirect('manage_lectures', course_id=course_id, module_id=module_id)


# Per-course enrolled student list
@login_required
def course_students(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    enrollments = (Enrollment.objects.filter(course=course)
                   .select_related('student').order_by('-enrolled_at'))
    return render(request, 'dashboard/course_students.html', {
        'course': course, 'enrollments': enrollments,
    })


# Every homework submission across a course's lectures, ungraded first so an
# instructor sees what needs attention before what's already settled.
@login_required
def course_submissions(request, course_id):
    course = get_object_or_404(Course, id=course_id, instructor=request.user)
    submissions = (Submission.objects.filter(lecture__module__course=course)
                   .select_related('student', 'lecture')
                   .order_by('graded_at', '-submitted_at'))
    return render(request, 'dashboard/course_submissions.html', {
        'course': course, 'submissions': submissions,
    })


@login_required
def grade_submission(request, submission_id):
    submission = get_object_or_404(
        Submission, id=submission_id, lecture__module__course__instructor=request.user)
    if submission.is_graded:
        return HttpResponseForbidden(_('This submission has already been graded.'))
    if request.method == 'POST':
        form = GradeForm(request.POST, instance=submission)
        if form.is_valid():
            graded = form.save(commit=False)
            graded.graded_at = timezone.now()
            graded.save()
            messages.success(
                request, _("Graded %(username)s's submission.") % {'username': submission.student.username})
    return redirect('course_submissions', course_id=submission.lecture.module.course_id)


# Instructor wallet: balance summary + full transaction ledger
@login_required
def instructor_wallet(request):
    if not request.user.is_instructor:
        return redirect('platform_home')
    wallet, _created = InstructorWallet.objects.get_or_create(instructor=request.user)
    transactions = wallet.transactions.all()
    payouts = wallet.payouts.all()
    revenue_distributions = (
        RevenueDistribution.objects.filter(instructor=request.user)
        .select_related('course', 'period')
    )

    next_payout_available_at = None
    last_request = payouts.order_by('-requested_at').first()
    if last_request and timezone.now() - last_request.requested_at < PAYOUT_COOLDOWN:
        next_payout_available_at = last_request.requested_at + PAYOUT_COOLDOWN

    return render(request, 'dashboard/wallet.html', {
        'wallet': wallet,
        'transactions': transactions,
        'payouts': payouts,
        'revenue_distributions': revenue_distributions,
        'form': PayoutRequestForm(),
        'next_payout_available_at': next_payout_available_at,
    })


PAYOUT_COOLDOWN = timedelta(days=7)


# Request a payout from available balance. The requested amount is reserved
# (moved from available_balance to pending_balance) immediately, so a second
# request can't be approved against money already promised to the first.
@login_required
def request_payout(request):
    if not request.user.is_instructor:
        return redirect('platform_home')
    wallet, _created = InstructorWallet.objects.get_or_create(instructor=request.user)

    if request.method == 'POST':
        last_request = wallet.payouts.order_by('-requested_at').first()
        if last_request and timezone.now() - last_request.requested_at < PAYOUT_COOLDOWN:
            next_available = last_request.requested_at + PAYOUT_COOLDOWN
            messages.error(
                request,
                _('You can request a payout once a week. Next available: %(date)s.')
                % {'date': next_available.strftime("%b %d, %Y")})
            return redirect('instructor_wallet')

        form = PayoutRequestForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                wallet = InstructorWallet.objects.select_for_update().get(pk=wallet.pk)
                amount = form.cleaned_data['amount']
                if amount <= wallet.available_balance:
                    wallet.available_balance -= amount
                    wallet.pending_balance += amount
                    wallet.save()
                    payout = form.save(commit=False)
                    payout.wallet = wallet
                    payout.save()
                    messages.success(request, _('Payout request submitted.'))
                else:
                    messages.error(request, _('Payout amount cannot exceed your available balance.'))
        else:
            messages.error(request, _('Please enter a valid payout amount.'))

    return redirect('instructor_wallet')


# Instructor-facing: request a new (child) track. Mirrors the wizard's
# "Add Module" list+form page -- an instructor's own past requests (with
# their current status) above a form to submit a new one. Approval/rejection
# is admin-only (see track_approval_queue/approve_track_request/
# reject_track_request below); this view never creates a real Track itself.
@login_required
def request_track(request):
    if not request.user.is_instructor:
        return redirect('platform_home')

    if request.method == 'POST':
        form = TrackRequestForm(request.POST)
        if form.is_valid():
            track_request = form.save(commit=False)
            track_request.instructor = request.user
            track_request.save()
            emails.send_track_request_notification(track_request)
            messages.success(
                request,
                _('Your request for "%(name)s" has been submitted for review.')
                % {'name': track_request.name})
            return redirect('request_track')
    else:
        form = TrackRequestForm()

    track_requests = TrackRequest.objects.filter(instructor=request.user).select_related('parent')
    return render(request, 'dashboard/request_track.html', {
        'form': form,
        'track_requests': track_requests,
    })


# 9. Admin Dashboard - KPIs and revenue over time
@admin_required
def admin_dashboard(request):
    succeeded_payments = Payment.objects.filter(status=Payment.Status.SUCCEEDED)
    totals = succeeded_payments.aggregate(
        total_revenue=Sum('total_amount'), platform_revenue=Sum('platform_amount'),
        instructor_revenue=Sum('instructor_amount'))
    total_paid_out = Payout.objects.filter(status=Payout.Status.PAID).aggregate(
        total=Sum('amount'))['total'] or 0

    monthly = (succeeded_payments.annotate(month=TruncMonth('created_at'))
               .values('month').annotate(revenue=Sum('total_amount')).order_by('month'))
    max_month_revenue = max([m['revenue'] for m in monthly], default=0) or 1

    # A course filed directly under a parent category (e.g. "Tech" instead
    # of "Web Development") has no course list of its own to appear in, so
    # it's silently invisible to students on every browse page. The
    # create-course form no longer allows this, but flag any course that
    # was already misfiled before that fix.
    misfiled_courses = (
        Course.objects.filter(track__parent__isnull=True)
        .select_related('track', 'instructor')
    )

    context = {
        'misfiled_courses': misfiled_courses,
        'total_students': User.objects.filter(is_student=True).count(),
        'total_instructors': User.objects.filter(is_instructor=True).count(),
        'total_courses': Course.objects.count(),
        'pending_courses_count': Course.objects.filter(status=Course.Status.PENDING_REVIEW).count(),
        'pending_track_requests_count': TrackRequest.objects.filter(
            status=TrackRequest.Status.PENDING).count(),
        'total_enrollments': Enrollment.objects.count(),
        'total_revenue': totals['total_revenue'] or 0,
        'platform_revenue': totals['platform_revenue'] or 0,
        'instructor_revenue': totals['instructor_revenue'] or 0,
        'total_paid_out': total_paid_out,
        'monthly_revenue': [
            {'label': m['month'].strftime('%b %Y'),
             'revenue': m['revenue'],
             'pct': int((m['revenue'] or 0) * 100 / max_month_revenue)}
            for m in monthly
        ],
        'due_subscription_periods_count': SubscriptionPeriod.objects.filter(
            status=SubscriptionPeriod.Status.OPEN, period_end__lte=timezone.now()).count(),
    }
    return render(request, 'dashboard/admin.html', context)


# Lets an admin trigger a real send of each of the three transactional email
# templates, to confirm formatting/links/attachments before relying on them
# in production. Same "no Shell on Render's free tier" workaround as
# run_subscription_distribution below -- this is the only way to fire one on
# demand without a shell.
def _report_test_email(request, sent, error, success_message):
    """emails.py's send_* functions now return (sent, error) instead of
    silently swallowing failures -- this is the one place that decides
    what the admin sees, so "sent successfully" in the UI only ever
    follows a confirmed, exception-free, backend-acknowledged send. On
    failure, the real reason (e.g. an SMTP auth error, a timeout, "0
    delivered") is shown directly -- this page is admin-only, so surfacing
    the raw error is safe and is the whole point of the tool."""
    if sent:
        messages.success(request, success_message)
    else:
        messages.error(request, _('Send failed: %(reason)s') % {'reason': error or _('unknown error')})


@admin_required
def send_test_emails(request):
    if request.method == 'POST':
        target = request.POST.get('target_email', '').strip()
        which = request.POST.get('which')

        if which in ('welcome', 'enrollment_confirmation', 'instructor_application_received',
                     'instructor_application_notification',
                     'instructor_welcome', 'instructor_rejection', 'course_approved', 'course_rejected',
                     'track_request_notification', 'track_request_approved', 'track_request_rejected',
                     'certificate') and not target:
            messages.error(request, _('Enter a target email address first.'))
            return redirect('send_test_emails')

        if which == 'welcome':
            sent, error = emails.send_welcome_email(request.user, to_email=target)
            _report_test_email(
                request, sent, error, _('Student welcome email sent to %(email)s.') % {'email': target})

        elif which == 'enrollment_confirmation':
            enrollment = Enrollment.objects.select_related('student', 'course__instructor').order_by(
                '-enrolled_at').first()
            if not enrollment:
                messages.error(
                    request,
                    _('No enrollments exist yet -- enroll in a course, then retry this test.'))
            else:
                sent, error = emails.send_enrollment_confirmation_email(enrollment, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Enrollment-confirmation email sent to %(email)s (using real enrollment in '
                      '"%(course)s" as sample data).') % {'email': target, 'course': enrollment.course.title})

        elif which == 'instructor_application_received':
            sent, error = emails.send_instructor_application_received_email(request.user, to_email=target)
            _report_test_email(
                request, sent, error,
                _('Instructor application-received email sent to %(email)s.') % {'email': target})

        elif which == 'instructor_application_notification':
            sent, error = emails.send_instructor_application_notification(request.user, to_email=target)
            _report_test_email(
                request, sent, error,
                _('Internal application-notification email sent to %(email)s (using your own '
                  'account\'s details as sample applicant data).') % {'email': target})

        elif which == 'instructor_welcome':
            sent, error = emails.send_instructor_welcome_email(request.user, to_email=target)
            _report_test_email(
                request, sent, error, _('Instructor welcome email sent to %(email)s.') % {'email': target})

        elif which == 'instructor_rejection':
            sent, error = emails.send_instructor_rejection_email(request.user, to_email=target)
            _report_test_email(
                request, sent, error, _('Instructor rejection email sent to %(email)s.') % {'email': target})

        elif which in ('course_approved', 'course_rejected'):
            course = Course.objects.select_related('instructor').order_by('-created_at').first()
            if not course:
                messages.error(
                    request,
                    _('No courses exist yet -- create one, then retry this test.'))
            elif which == 'course_approved':
                sent, error = emails.send_course_approved_email(course, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Course-approved email sent to %(email)s (using real course '
                      '"%(course)s" as sample data).') % {'email': target, 'course': course.title})
            else:
                sent, error = emails.send_course_rejected_email(course, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Course-rejected email sent to %(email)s (using real course '
                      '"%(course)s" as sample data).') % {'email': target, 'course': course.title})

        elif which == 'track_request_notification':
            track_request = TrackRequest.objects.select_related('instructor', 'parent').order_by(
                '-created_at').first()
            if not track_request:
                messages.error(
                    request,
                    _('No track requests exist yet -- submit one, then retry this test.'))
            else:
                sent, error = emails.send_track_request_notification(track_request, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Internal track-request notification sent to %(email)s (using real request '
                      '"%(track)s" as sample data).') % {'email': target, 'track': track_request.name})

        elif which == 'track_request_approved':
            track_request = (
                TrackRequest.objects.filter(status=TrackRequest.Status.APPROVED)
                .select_related('instructor', 'track').order_by('-created_at').first()
            )
            if not track_request:
                messages.error(
                    request,
                    _('No track requests have been approved yet -- approve one, then retry this test.'))
            else:
                sent, error = emails.send_track_request_approved_email(track_request, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Track-approved email sent to %(email)s (using real request '
                      '"%(track)s" as sample data).') % {'email': target, 'track': track_request.name})

        elif which == 'track_request_rejected':
            track_request = (
                TrackRequest.objects.filter(status=TrackRequest.Status.REJECTED)
                .select_related('instructor').order_by('-created_at').first()
            )
            if not track_request:
                messages.error(
                    request,
                    _('No track requests have been rejected yet -- reject one, then retry this test.'))
            else:
                sent, error = emails.send_track_request_rejected_email(track_request, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Track-rejected email sent to %(email)s (using real request '
                      '"%(track)s" as sample data).') % {'email': target, 'track': track_request.name})

        elif which == 'certificate':
            certificate = (
                Certificate.objects.select_related(
                    'enrollment__student', 'enrollment__course__instructor', 'enrollment__course__track')
                .order_by('-issued_at').first()
            )
            if not certificate:
                messages.error(
                    request,
                    _('No certificates have been issued yet -- complete a course to generate '
                      'one, then retry this test.'))
            else:
                sent, error = emails.send_certificate_email(certificate, to_email=target)
                _report_test_email(
                    request, sent, error,
                    _('Certificate email sent to %(email)s (using real certificate data for '
                      '"%(course)s").') % {'email': target, 'course': certificate.enrollment.course.title})

        elif which in ('password_reset_student', 'password_reset_instructor'):
            # Always goes to the logged-in admin's own address -- a real,
            # working reset link that can't be redirected to an arbitrary
            # address. The Student/Instructor choice here only picks which
            # template set to preview; it doesn't touch the account's real
            # is_instructor flag or the real reset flow's own role
            # detection (RoleAwarePasswordResetForm, used by the actual
            # /password-reset/ page).
            if not request.user.email:
                messages.error(request, _('Your admin account has no email address on file.'))
            else:
                sent, error = emails.send_password_reset_preview(
                    request.user, request, as_instructor=(which == 'password_reset_instructor'))
                _report_test_email(
                    request, sent, error,
                    _('Password reset email (%(role)s template) sent to your own address '
                      '(%(email)s) -- this is a real, working reset link.') % {
                        'role': _('Instructor') if which == 'password_reset_instructor' else _('Student'),
                        'email': request.user.email,
                    })

        return redirect('send_test_emails')

    return render(request, 'dashboard/admin_test_emails.html', {'default_email': request.user.email})


# Manually kicks off the subscription revenue-distribution job. The free
# Render plan has no Cron Jobs and no Shell, so there's no automatic
# scheduler wired up -- this button is the only way to actually run it in
# production until the plan is upgraded.
@admin_required
def run_subscription_distribution(request):
    if request.method == 'POST':
        call_command('distribute_subscription_revenue')
        messages.success(request, _('Subscription revenue distribution ran successfully.'))
    return redirect('admin_subscription_revenue')


# Per-period breakdown: the pool, watch-time by course, each instructor's
# share, and the platform cut. This is the actual detail page -- the
# dashboard card only shows a count and a button to run the job.
@admin_required
def admin_subscription_revenue(request):
    periods = (
        SubscriptionPeriod.objects.select_related('subscription__student', 'subscription__plan')
        .prefetch_related('distributions__course', 'distributions__instructor')
        .order_by('-period_start')[:50]
    )
    due_count = SubscriptionPeriod.objects.filter(
        status=SubscriptionPeriod.Status.OPEN, period_end__lte=timezone.now()).count()
    return render(request, 'dashboard/admin_subscription_revenue.html', {
        'periods': periods,
        'due_subscription_periods_count': due_count,
    })


# Course approval queue -- admin approves or rejects, instructors cannot self-publish
@admin_required
def course_approval_queue(request):
    courses = (Course.objects.filter(status=Course.Status.PENDING_REVIEW)
               .select_related('instructor', 'track').order_by('created_at'))
    return render(request, 'dashboard/course_approval_queue.html', {'courses': courses})


@admin_required
def approve_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, status=Course.Status.PENDING_REVIEW)
    if request.method == 'POST':
        course.status = Course.Status.PUBLISHED
        course.rejection_reason = ''
        course.save()
        emails.send_course_approved_email(course)
        messages.success(request, _('%(title)s approved and published.') % {'title': course.title})
    return redirect('course_approval_queue')


@admin_required
def reject_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, status=Course.Status.PENDING_REVIEW)
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            # A rejection with no explanation isn't actionable for the
            # instructor -- require one instead of silently sending a
            # generic "no reason given" email every time someone forgets
            # to fill in the field.
            messages.error(request, _('Please enter a rejection reason before rejecting %(title)s.')
                            % {'title': course.title})
            return redirect('course_approval_queue')
        course.status = Course.Status.REJECTED
        course.rejection_reason = reason
        course.save()
        emails.send_course_rejected_email(course)
        messages.success(request, _('%(title)s rejected.') % {'title': course.title})
    return redirect('course_approval_queue')


# Pending registrations awaiting approval, plus separate Students/Instructors
# management tables for everyone already approved.
@admin_required
def admin_users(request):
    pending_users = User.objects.filter(is_approved=False).order_by('-date_joined')
    students = User.objects.filter(is_student=True, is_approved=True).order_by('-date_joined')
    instructors = User.objects.filter(is_instructor=True, is_approved=True).order_by('-date_joined')
    return render(request, 'dashboard/admin_users.html', {
        'pending_users': pending_users,
        'students': students,
        'instructors': instructors,
    })


@admin_required
def approve_user(request, user_id):
    user = get_object_or_404(User, id=user_id, is_approved=False)
    if request.method == 'POST':
        if user.is_international_instructor and not user.payoneer_account:
            messages.error(
                request,
                _('%(username)s can\'t be approved yet -- a Payoneer account is required for '
                  'instructors based outside Egypt.') % {'username': user.username})
        else:
            user.is_approved = True
            user.save()
            if user.is_instructor:
                emails.send_instructor_welcome_email(user)
            messages.success(request, _('%(username)s has been approved.') % {'username': user.username})
    return redirect('admin_users')


@admin_required
def reject_user(request, user_id):
    user = get_object_or_404(User, id=user_id, is_approved=False)
    if request.method == 'POST':
        username = user.username
        is_instructor = user.is_instructor
        try:
            user.delete()
        except ProtectedError:
            messages.error(
                request,
                _('%(username)s has existing activity on the platform and can\'t be rejected.')
                % {'username': username})
        else:
            # .delete() clears the in-memory instance's pk but leaves every
            # other field (email, username, name) intact -- still safe to
            # read for the notification below.
            if is_instructor:
                emails.send_instructor_rejection_email(user)
            messages.success(
                request, _('%(username)s\'s registration was rejected and removed.') % {'username': username})
    return redirect('admin_users')


@admin_required
def delete_user(request, user_id):
    user = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        if user == request.user:
            messages.error(request, _("You can't delete your own account."))
        elif user.is_superuser:
            messages.error(request, _("Admin accounts can't be deleted from here."))
        else:
            username = user.username
            try:
                user.delete()
                messages.success(request, _('%(username)s was permanently deleted.') % {'username': username})
            except ProtectedError:
                messages.error(
                    request,
                    _('%(username)s has payment or revenue history and can\'t be deleted.')
                    % {'username': username})
    return redirect('admin_users')


# Payments table
@admin_required
def admin_payments(request):
    payments = Payment.objects.select_related('student', 'course').order_by('-created_at')
    return render(request, 'dashboard/admin_payments.html', {'payments': payments})


# Payout requests -- approve / reject / mark paid
@admin_required
def admin_payouts(request):
    payouts = Payout.objects.select_related('wallet__instructor').order_by('-requested_at')
    return render(request, 'dashboard/admin_payouts.html', {'payouts': payouts})


@admin_required
def approve_payout(request, payout_id):
    payout = get_object_or_404(Payout, id=payout_id, status=Payout.Status.REQUESTED)
    if request.method == 'POST':
        payout.status = Payout.Status.APPROVED
        payout.save()
    return redirect('admin_payouts')


@admin_required
def reject_payout(request, payout_id):
    payout = get_object_or_404(Payout, id=payout_id, status=Payout.Status.REQUESTED)
    if request.method == 'POST':
        with transaction.atomic():
            wallet = InstructorWallet.objects.select_for_update().get(pk=payout.wallet_id)
            wallet.pending_balance -= payout.amount
            wallet.available_balance += payout.amount
            wallet.save()
            payout.status = Payout.Status.REJECTED
            payout.admin_note = request.POST.get('admin_note', '')
            payout.processed_at = timezone.now()
            payout.save()
    return redirect('admin_payouts')


@admin_required
def mark_payout_paid(request, payout_id):
    payout = get_object_or_404(Payout, id=payout_id, status=Payout.Status.APPROVED)
    if request.method == 'POST':
        with transaction.atomic():
            wallet = InstructorWallet.objects.select_for_update().get(pk=payout.wallet_id)
            wallet.pending_balance -= payout.amount
            wallet.total_withdrawn += payout.amount
            wallet.save()
            WalletTransaction.objects.create(
                wallet=wallet, type=WalletTransaction.Type.WITHDRAWAL,
                amount=payout.amount, balance_after=wallet.available_balance,
                note=f'Payout #{payout.id}')
            payout.status = Payout.Status.PAID
            payout.processed_at = timezone.now()
            payout.save()
    return redirect('admin_payouts')


# Track CRUD
@admin_required
def admin_tracks(request):
    # Parents first, each immediately followed by its own children, so the
    # nested taxonomy reads as a tree instead of an arbitrarily interleaved list.
    parents = Track.objects.filter(parent__isnull=True).order_by('order', 'name').prefetch_related(
        Prefetch('children', queryset=Track.objects.order_by('order', 'name'))
    )
    tracks = []
    for parent in parents:
        tracks.append(parent)
        tracks.extend(parent.children.all())

    if request.method == 'POST':
        form = TrackForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect('admin_tracks')
    else:
        form = TrackForm()
    return render(request, 'dashboard/admin_tracks.html', {'tracks': tracks, 'form': form})


@admin_required
def toggle_track_active(request, track_id):
    track = get_object_or_404(Track, id=track_id)
    if request.method == 'POST':
        track.is_active = not track.is_active
        track.save()
    return redirect('admin_tracks')


# Track approval queue -- admin approves or rejects, instructors cannot
# create a Track directly. Same shape as course_approval_queue above.
@admin_required
def track_approval_queue(request):
    track_requests = (
        TrackRequest.objects.filter(status=TrackRequest.Status.PENDING)
        .select_related('instructor', 'parent').order_by('created_at'))
    return render(request, 'dashboard/track_approval_queue.html', {'track_requests': track_requests})


@admin_required
def approve_track_request(request, request_id):
    track_request = get_object_or_404(
        TrackRequest, id=request_id, status=TrackRequest.Status.PENDING)
    if request.method == 'POST':
        try:
            # Wrapped in its own atomic block so the IntegrityError below
            # only rolls back this one INSERT (via a savepoint) instead of
            # poisoning any transaction the caller is already inside.
            with transaction.atomic():
                track = Track.objects.create(
                    parent=track_request.parent, name=track_request.name)
        except IntegrityError:
            # Track.name is globally unique -- someone else (another
            # approved request, or a manual admin_tracks entry) already
            # claimed this exact name since the request was submitted.
            messages.error(
                request,
                _('A track named "%(name)s" already exists. Reject this request or rename it first.')
                % {'name': track_request.name})
            return redirect('track_approval_queue')
        track_request.status = TrackRequest.Status.APPROVED
        track_request.track = track
        track_request.rejection_reason = ''
        track_request.save()
        emails.send_track_request_approved_email(track_request)
        messages.success(request, _('"%(name)s" approved and is now live.') % {'name': track_request.name})
    return redirect('track_approval_queue')


@admin_required
def reject_track_request(request, request_id):
    track_request = get_object_or_404(
        TrackRequest, id=request_id, status=TrackRequest.Status.PENDING)
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            # Same requirement as reject_course -- a rejection with no
            # explanation isn't actionable for the instructor.
            messages.error(
                request, _('Please enter a rejection reason before rejecting "%(name)s".')
                % {'name': track_request.name})
            return redirect('track_approval_queue')
        track_request.status = TrackRequest.Status.REJECTED
        track_request.rejection_reason = reason
        track_request.save()
        emails.send_track_request_rejected_email(track_request)
        messages.success(request, _('"%(name)s" rejected.') % {'name': track_request.name})
    return redirect('track_approval_queue')


