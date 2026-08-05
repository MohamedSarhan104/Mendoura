import hashlib
import hmac
import json
import random
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.translation import override as translation_override

from . import ai_coach, auto_translate, emails, paymob
from .models import (
    AIConversation, AIMessage, Certificate, Choice, Course, Enrollment, InstructorWallet, Lecture,
    LectureProgress, LegalDocument, LegalSection, Module, Payment, Payout, Plan, Question, Quiz,
    QuizAttempt, Resource, RevenueDistribution, Review, Subscription, SubscriptionPeriod, Submission,
    Track, TrackRequest, User, WalletTransaction, WatchEvent,
)
from .money import calculate_split


class SplitCalculationTests(TestCase):
    """The money math is the highest-risk part of this project."""

    def test_no_lost_cents(self):
        """The most important test in the whole project."""
        for cents in range(1, 100_00):  # $0.01 -> $100.00
            total = Decimal(cents) / Decimal('100')
            for pct in (Decimal('70.00'), Decimal('50.00')):
                inst, plat = calculate_split(total, pct)
                assert inst + plat == total  # nothing lost to rounding
                assert inst >= 0 and plat >= 0

    def test_awkward_rounding(self):
        # 19.99 x 0.70 = 13.993 -- must not raise, must not lose a cent
        self.assertEqual(
            calculate_split(Decimal('20.00'), Decimal('70.00')),
            (Decimal('14.00'), Decimal('6.00')),
        )

    def test_fifty_fifty_odd_cent(self):
        # 9.99 / 2 = 4.995 -- the half-cent goes to the platform
        self.assertEqual(
            calculate_split(Decimal('9.99'), Decimal('50.00')),
            (Decimal('4.99'), Decimal('5.00')),
        )


class PaymentModelTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='instructor1', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='student1', password='pw', is_student=True)
        self.track = Track.objects.create(name='Web Development')
        self.course = Course.objects.create(
            instructor=self.instructor,
            track=self.track,
            title='Django Basics',
            description='Learn Django',
            production_type=Course.ProductionType.FULL,
            price=Decimal('20.00'),
        )

    def test_payment_snapshots_split_at_creation(self):
        payment = Payment.objects.create(
            student=self.student, course=self.course, total_amount=Decimal('20.00'))
        self.assertEqual(payment.production_type_at_purchase, Course.ProductionType.FULL)
        self.assertEqual(payment.instructor_share_percentage, Decimal('70.00'))
        self.assertEqual(payment.instructor_amount, Decimal('14.00'))
        self.assertEqual(payment.platform_amount, Decimal('6.00'))
        self.assertEqual(payment.instructor_amount + payment.platform_amount, payment.total_amount)

    def test_payment_frozen_fields_are_immutable(self):
        payment = Payment.objects.create(
            student=self.student, course=self.course, total_amount=Decimal('20.00'))
        payment.total_amount = Decimal('999.00')
        with self.assertRaises(ValidationError):
            payment.save()

    def test_payment_snapshot_survives_course_production_type_change(self):
        payment = Payment.objects.create(
            student=self.student, course=self.course, total_amount=Decimal('20.00'))
        # A later course, or a hypothetical future production_type change, must
        # never retroactively alter a historical payment's snapshot.
        self.assertEqual(payment.instructor_share_percentage, Decimal('70.00'))
        payment.refresh_from_db()
        self.assertEqual(payment.instructor_share_percentage, Decimal('70.00'))

    def test_production_type_locked_after_first_successful_sale(self):
        Payment.objects.create(
            student=self.student, course=self.course, total_amount=Decimal('20.00'),
            status=Payment.Status.SUCCEEDED)
        self.course.production_type = Course.ProductionType.SCRIPT_ONLY
        with self.assertRaises(ValidationError):
            self.course.save()

    def test_production_type_changeable_before_any_sale(self):
        self.course.production_type = Course.ProductionType.SCRIPT_ONLY
        self.course.save()  # no successful payment yet -- must not raise
        self.course.refresh_from_db()
        self.assertEqual(self.course.production_type, Course.ProductionType.SCRIPT_ONLY)


class WalletTransactionLedgerTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='instructor2', password='pw', is_instructor=True)
        self.wallet = InstructorWallet.objects.create(instructor=self.instructor)

    def test_wallet_credit_updates_balances_and_ledger(self):
        credit = Decimal('14.00')
        self.wallet.available_balance += credit
        self.wallet.total_earnings += credit
        self.wallet.save()
        txn = WalletTransaction.objects.create(
            wallet=self.wallet, type=WalletTransaction.Type.SALE_CREDIT,
            amount=credit, balance_after=self.wallet.available_balance,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, credit)
        self.assertEqual(txn.balance_after, self.wallet.available_balance)

    def test_ledger_rows_are_append_only(self):
        txn = WalletTransaction.objects.create(
            wallet=self.wallet, type=WalletTransaction.Type.SALE_CREDIT,
            amount=Decimal('10.00'), balance_after=Decimal('10.00'),
        )
        txn.amount = Decimal('999.00')
        with self.assertRaises(ValidationError):
            txn.save()
        with self.assertRaises(ValidationError):
            txn.delete()


class LectureAccessControlTests(TestCase):
    """An unenrolled student must not reach lecture content by guessing a URL."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='inst', password='pw', is_instructor=True)
        self.enrolled_student = User.objects.create_user(
            username='enrolled', password='pw', is_student=True)
        self.outside_student = User.objects.create_user(
            username='outsider', password='pw', is_student=True)
        track = Track.objects.create(name='Web Development')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Django Basics',
            description='...', production_type=Course.ProductionType.FULL,
            price=Decimal('0.00'), is_free=True, status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=self.course, title='Module 1')
        self.preview_lecture = Lecture.objects.create(
            module=module, title='Intro', is_preview=True)
        self.locked_lecture = Lecture.objects.create(
            module=module, title='Deep Dive', is_preview=False)
        Enrollment.objects.create(student=self.enrolled_student, course=self.course)

    def _player_url(self, lecture):
        return reverse('course_player', args=[self.course.id, lecture.id])

    def test_anonymous_user_can_watch_preview_lecture(self):
        response = self.client.get(self._player_url(self.preview_lecture))
        self.assertEqual(response.status_code, 200)

    def test_anonymous_user_redirected_to_login_for_locked_lecture(self):
        response = self.client.get(self._player_url(self.locked_lecture))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_unenrolled_student_cannot_reach_locked_lecture(self):
        self.client.force_login(self.outside_student)
        response = self.client.get(self._player_url(self.locked_lecture))
        self.assertEqual(response.status_code, 403)

    def test_unenrolled_student_can_still_watch_preview_lecture(self):
        self.client.force_login(self.outside_student)
        response = self.client.get(self._player_url(self.preview_lecture))
        self.assertEqual(response.status_code, 200)

    def test_enrolled_student_can_reach_locked_lecture(self):
        self.client.force_login(self.enrolled_student)
        response = self.client.get(self._player_url(self.locked_lecture))
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES={
    # Certificate PDFs are saved through the file storage backend (Cloudinary
    # in production); swap in an in-memory backend so these tests don't
    # attempt a real network call with empty credentials.
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class EnrollmentAndReviewTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='inst2', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='stud2', password='pw', is_student=True)
        track = Track.objects.create(name='Data Science & AI')
        self.free_course = Course.objects.create(
            instructor=self.instructor, track=track, title='Intro to Pandas',
            description='...', production_type=Course.ProductionType.SCRIPT_ONLY,
            price=Decimal('0.00'), is_free=True, status=Course.Status.PUBLISHED,
        )

    def test_enroll_free_course_is_instant(self):
        self.client.force_login(self.student)
        self.client.post(reverse('enroll_course', args=[self.free_course.id]))
        self.assertTrue(
            Enrollment.objects.filter(student=self.student, course=self.free_course).exists())

    def test_enroll_free_course_sends_confirmation_email(self):
        # Regression test: enrolling (any path -- this is the free-course
        # branch of enroll_course) previously sent no email to the student
        # at all, only an internal admin notification for paid purchases.
        student = User.objects.create_user(
            username='free_enroll_stud', password='pw', is_student=True,
            email='free_enroll_stud@example.com')
        self.client.force_login(student)
        self.client.post(reverse('enroll_course', args=[self.free_course.id]))
        sent = next(m for m in mail.outbox if m.to == ['free_enroll_stud@example.com'])
        self.assertIn(self.free_course.title, sent.subject)
        self.assertIn(self.free_course.title, sent.body)
        self.assertIn(self.instructor.username, sent.body)

    def test_only_enrolled_students_can_review(self):
        self.client.force_login(self.student)
        self.client.post(reverse('add_review', args=[self.free_course.id]),
                          {'rating': 5, 'comment': 'Great!'})
        self.assertFalse(Review.objects.filter(student=self.student).exists())

        Enrollment.objects.create(student=self.student, course=self.free_course)
        self.client.post(reverse('add_review', args=[self.free_course.id]),
                          {'rating': 5, 'comment': 'Great!'})
        self.assertTrue(Review.objects.filter(student=self.student).exists())

    def test_completing_all_lectures_issues_certificate(self):
        module = Module.objects.create(course=self.free_course, title='Module 1')
        lecture = Lecture.objects.create(module=module, title='Only Lecture')
        enrollment = Enrollment.objects.create(student=self.student, course=self.free_course)

        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.free_course.id, lecture.id]))

        self.assertTrue(Certificate.objects.filter(enrollment=enrollment).exists())

    def test_certificate_not_issued_before_completion(self):
        module = Module.objects.create(course=self.free_course, title='Module 1')
        Lecture.objects.create(module=module, title='Lecture 1')
        Lecture.objects.create(module=module, title='Lecture 2')
        enrollment = Enrollment.objects.create(student=self.student, course=self.free_course)

        self.assertIsNone(enrollment.issue_certificate_if_complete())
        self.assertFalse(Certificate.objects.filter(enrollment=enrollment).exists())

    def test_certificate_has_pdf_and_unique_uuid(self):
        module = Module.objects.create(course=self.free_course, title='Module 1')
        lecture = Lecture.objects.create(module=module, title='Only Lecture')
        enrollment = Enrollment.objects.create(student=self.student, course=self.free_course)

        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.free_course.id, lecture.id]))

        certificate = Certificate.objects.get(enrollment=enrollment)
        self.assertTrue(certificate.pdf_file.name)
        self.assertTrue(certificate.pdf_file.read().startswith(b'%PDF'))
        self.assertIsNotNone(certificate.uuid)

    def test_completing_already_complete_course_does_not_duplicate_certificate(self):
        module = Module.objects.create(course=self.free_course, title='Module 1')
        lecture = Lecture.objects.create(module=module, title='Only Lecture')
        enrollment = Enrollment.objects.create(student=self.student, course=self.free_course)

        self.client.force_login(self.student)
        url = reverse('mark_lecture_complete', args=[self.free_course.id, lecture.id])
        self.client.post(url)
        self.client.post(url)

        self.assertEqual(Certificate.objects.filter(enrollment=enrollment).count(), 1)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CertificateVerificationTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='cert_inst', password='pw', is_instructor=True,
            first_name='Jane', last_name='Doe')
        self.student = User.objects.create_user(
            username='cert_student', password='pw', is_student=True,
            first_name='John', last_name='Smith')
        track = Track.objects.create(name='Certificates Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Certificate Course',
            description='...', production_type=Course.ProductionType.SCRIPT_ONLY,
            price=Decimal('0.00'), is_free=True, status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=self.course, title='Module 1')
        self.lecture = Lecture.objects.create(module=module, title='Only Lecture')
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def _complete_course(self):
        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.client.logout()
        return Certificate.objects.get(enrollment=self.enrollment)

    def test_verify_url_is_public_and_shows_names(self):
        certificate = self._complete_course()

        response = self.client.get(reverse('certificate_verify', args=[certificate.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'John Smith')
        self.assertContains(response, 'Jane Doe')
        self.assertContains(response, 'Certificate Course')

    def test_download_returns_pdf(self):
        certificate = self._complete_course()

        response = self.client.get(reverse('certificate_download', args=[certificate.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_verify_unknown_uuid_returns_404(self):
        import uuid as uuid_module
        response = self.client.get(reverse('certificate_verify', args=[uuid_module.uuid4()]))
        self.assertEqual(response.status_code, 404)


class InstructorIsolationTests(TestCase):
    """An instructor must not see another instructor's courses, students, or wallet."""

    def setUp(self):
        self.track = Track.objects.create(name='Cloud & DevOps')
        self.owner = User.objects.create_user(username='owner', password='pw', is_instructor=True)
        self.intruder = User.objects.create_user(username='intruder', password='pw', is_instructor=True)
        self.course = Course.objects.create(
            instructor=self.owner, track=self.track, title='Owner Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
        )
        self.module = Module.objects.create(course=self.course, title='M1')

    def test_cannot_manage_modules_of_anothers_course(self):
        self.client.force_login(self.intruder)
        response = self.client.get(reverse('manage_modules', args=[self.course.id]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_manage_lectures_of_anothers_course(self):
        self.client.force_login(self.intruder)
        response = self.client.get(
            reverse('manage_lectures', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_view_students_of_anothers_course(self):
        self.client.force_login(self.intruder)
        response = self.client.get(reverse('course_students', args=[self.course.id]))
        self.assertEqual(response.status_code, 404)

    def test_wallet_view_is_scoped_to_the_logged_in_instructor(self):
        owner_wallet = InstructorWallet.objects.create(instructor=self.owner, available_balance=Decimal('42.00'))
        InstructorWallet.objects.create(instructor=self.intruder, available_balance=Decimal('0.00'))

        self.client.force_login(self.intruder)
        response = self.client.get(reverse('instructor_wallet'))
        self.assertNotContains(response, '42.00')

    def test_cannot_edit_or_delete_anothers_course(self):
        self.client.force_login(self.intruder)
        self.assertEqual(
            self.client.get(reverse('edit_course', args=[self.course.id])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse('delete_course', args=[self.course.id])).status_code, 404)
        self.course.refresh_from_db()
        self.assertNotEqual(self.course.status, Course.Status.ARCHIVED)

    def test_cannot_edit_or_delete_anothers_module(self):
        self.client.force_login(self.intruder)
        self.assertEqual(
            self.client.get(reverse('edit_module', args=[self.course.id, self.module.id])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse('delete_module', args=[self.course.id, self.module.id])).status_code, 404)
        self.assertTrue(Module.objects.filter(id=self.module.id).exists())

    def test_cannot_edit_or_delete_anothers_lecture(self):
        lecture = Lecture.objects.create(module=self.module, title='L1')
        self.client.force_login(self.intruder)
        self.assertEqual(self.client.get(reverse('edit_lecture', args=[lecture.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse('delete_lecture', args=[lecture.id])).status_code, 404)
        self.assertTrue(Lecture.objects.filter(id=lecture.id).exists())

    def test_cannot_delete_anothers_resource(self):
        lecture = Lecture.objects.create(module=self.module, title='L1')
        resource = Resource.objects.create(lecture=lecture, title='Slides')
        self.client.force_login(self.intruder)
        response = self.client.post(reverse('delete_resource', args=[resource.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Resource.objects.filter(id=resource.id).exists())


class PayoutRequestTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='payout_inst', password='pw', is_instructor=True)
        self.wallet = InstructorWallet.objects.create(
            instructor=self.instructor, available_balance=Decimal('20.00'))

    def test_cannot_request_more_than_available_balance(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_payout'), {'amount': '50.00', 'method': 'bank'})
        self.assertFalse(Payout.objects.filter(wallet=self.wallet).exists())

    def test_can_request_up_to_available_balance(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_payout'), {'amount': '20.00', 'method': 'bank'})
        self.assertTrue(Payout.objects.filter(wallet=self.wallet, amount=Decimal('20.00')).exists())

    def test_requesting_reserves_the_amount_so_it_cannot_be_double_spent(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_payout'), {'amount': '20.00', 'method': 'bank'})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal('0.00'))
        self.assertEqual(self.wallet.pending_balance, Decimal('20.00'))

        # A second request against the now-empty available balance must fail.
        self.client.post(reverse('request_payout'), {'amount': '20.00', 'method': 'bank'})
        self.assertEqual(Payout.objects.filter(wallet=self.wallet).count(), 1)

    def test_second_request_within_a_week_is_blocked_even_with_balance(self):
        self.wallet.available_balance = Decimal('100.00')
        self.wallet.save()
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_payout'), {'amount': '10.00', 'method': 'bank'})
        self.assertEqual(Payout.objects.filter(wallet=self.wallet).count(), 1)

        # Balance is there, but the weekly cooldown should still block a
        # second request the same day.
        self.client.post(reverse('request_payout'), {'amount': '10.00', 'method': 'bank'})
        self.assertEqual(Payout.objects.filter(wallet=self.wallet).count(), 1)

    def test_request_allowed_again_after_a_week(self):
        self.wallet.available_balance = Decimal('100.00')
        self.wallet.save()
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_payout'), {'amount': '10.00', 'method': 'bank'})

        old_payout = Payout.objects.get(wallet=self.wallet)
        old_payout.requested_at = timezone.now() - timedelta(days=8)
        old_payout.save()

        self.client.post(reverse('request_payout'), {'amount': '10.00', 'method': 'bank'})
        self.assertEqual(Payout.objects.filter(wallet=self.wallet).count(), 2)


class CourseCreationTrackScopeTests(TestCase):
    """A course must only ever be filed under a leaf track -- a parent
    category like 'Tech' has no course list of its own, so a course
    assigned to one would silently never appear on any student browse page."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='track_scope_inst', password='pw', is_instructor=True)
        self.parent = Track.objects.create(name='Tech')
        self.child = Track.objects.create(name='Web Development', parent=self.parent)

    def test_create_course_form_only_offers_leaf_tracks(self):
        from .forms import CourseCreationForm
        form = CourseCreationForm()
        track_ids = set(form.fields['track'].queryset.values_list('id', flat=True))
        self.assertIn(self.child.id, track_ids)
        self.assertNotIn(self.parent.id, track_ids)

    def test_posting_a_parent_track_is_rejected(self):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('create_course'), {
            'title': 'Broken Course', 'description': 'x', 'track': self.parent.id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '0.00',
        })
        self.assertFalse(Course.objects.filter(title='Broken Course').exists())
        self.assertEqual(response.status_code, 200)  # re-renders the form with errors


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CoursePosterTests(TestCase):
    """The video-player poster: a 1280x720 composite of the course's
    thumbnail (or a branded placeholder) with the title and instructor's
    name burned in server-side -- see courses/poster.py."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='poster_test_inst', password='pw', is_instructor=True,
            first_name='Nour', last_name='Adel')
        self.parent_track = Track.objects.create(name='Poster Parent')
        self.track = Track.objects.create(name='Poster Child', parent=self.parent_track)

    def _uploaded_photo(self, size=(1200, 800), color=(90, 110, 200), name='cover.jpg'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new('RGB', size, color).save(buf, format='JPEG')
        return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')

    def _create_course(self, thumbnail=None, title='Sample Course'):
        return Course.objects.create(
            instructor=self.instructor, track=self.track, title=title, description='d',
            production_type=Course.ProductionType.FULL, status=Course.Status.PUBLISHED,
            thumbnail=thumbnail,
        )

    def test_build_poster_image_without_thumbnail_uses_placeholder(self):
        from PIL import Image
        import io
        from courses import poster

        course = self._create_course()
        image_bytes = poster.build_poster_image(course)
        image = Image.open(io.BytesIO(image_bytes))
        self.assertEqual(image.size, (poster.WIDTH, poster.HEIGHT))
        self.assertEqual(image.format, 'JPEG')

    def test_build_poster_image_with_thumbnail_crops_to_16_9(self):
        from PIL import Image
        import io
        from courses import poster

        course = self._create_course(thumbnail=self._uploaded_photo(size=(2000, 1000)))
        image_bytes = poster.build_poster_image(course)
        image = Image.open(io.BytesIO(image_bytes))
        self.assertEqual(image.size, (poster.WIDTH, poster.HEIGHT))

    def test_very_long_title_does_not_crash_and_stays_bounded(self):
        from PIL import Image
        import io
        from courses import poster

        course = self._create_course(
            title='A ' + 'Very ' * 40 + 'Long Course Title That Should Wrap And Truncate')
        image_bytes = poster.build_poster_image(course)
        image = Image.open(io.BytesIO(image_bytes))
        self.assertEqual(image.size, (poster.WIDTH, poster.HEIGHT))

    def test_generate_poster_saves_to_poster_image_field(self):
        course = self._create_course()
        self.assertFalse(course.poster_image)
        course.generate_poster()
        self.assertTrue(course.poster_image)
        course.poster_image.open('rb')
        data = course.poster_image.read()
        course.poster_image.close()
        self.assertTrue(data.startswith(b'\xff\xd8'))  # JPEG magic bytes

    def test_create_course_view_generates_poster(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), {
            'title': 'Freshly Created Course', 'description': 'd', 'track': self.track.id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '0.00', 'is_free': 'on',
        })
        course = Course.objects.get(title='Freshly Created Course')
        self.assertTrue(course.poster_image)

    def test_edit_course_unrelated_field_does_not_regenerate_poster(self):
        course = self._create_course()
        course.generate_poster()
        original_name = course.poster_image.name

        self.client.force_login(self.instructor)
        self.client.post(reverse('edit_course', args=[course.id]), {
            'title': course.title, 'description': course.description, 'track': self.track.id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '25.00',
        })
        course.refresh_from_db()
        self.assertEqual(course.poster_image.name, original_name)

    def test_edit_course_title_change_regenerates_poster(self):
        course = self._create_course()
        course.generate_poster()
        original_name = course.poster_image.name

        self.client.force_login(self.instructor)
        self.client.post(reverse('edit_course', args=[course.id]), {
            'title': 'A Brand New Title', 'description': course.description, 'track': self.track.id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '0.00',
        })
        course.refresh_from_db()
        self.assertNotEqual(course.poster_image.name, original_name)

    def test_edit_course_thumbnail_change_regenerates_poster(self):
        course = self._create_course()
        course.generate_poster()
        original_name = course.poster_image.name

        self.client.force_login(self.instructor)
        self.client.post(reverse('edit_course', args=[course.id]), {
            'title': course.title, 'description': course.description, 'track': self.track.id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '0.00',
            'thumbnail': self._uploaded_photo(name='new_cover.jpg'),
        })
        course.refresh_from_db()
        self.assertNotEqual(course.poster_image.name, original_name)

    def test_player_page_backfills_missing_poster_lazily(self):
        course = self._create_course()
        module = Module.objects.create(course=course, title='M1')
        lecture = Lecture.objects.create(
            module=module, title='L1', video_url='https://example.com/v', is_preview=True)
        self.assertFalse(course.poster_image)

        response = self.client.get(reverse('course_player', args=[course.id, lecture.id]))
        self.assertEqual(response.status_code, 200)
        course.refresh_from_db()
        self.assertTrue(course.poster_image)

    def test_player_page_renders_poster_cover_with_lazy_video_src(self):
        course = self._create_course()
        course.generate_poster()
        module = Module.objects.create(course=course, title='M1')
        lecture = Lecture.objects.create(
            module=module, title='L1', video_url='https://example.com/v', is_preview=True)

        response = self.client.get(reverse('course_player', args=[course.id, lecture.id]))
        self.assertContains(response, 'id="poster-cover"')
        self.assertContains(response, course.poster_image.url)
        self.assertContains(response, 'data-src="https://example.com/v"')
        # "data-src=" contains "src=" as a substring, so this checks for a
        # real, separate src="..." attribute (leading space) rather than
        # naively asserting the whole URL string is absent.
        self.assertNotContains(response, ' src="https://example.com/v"')

    def test_player_page_without_video_source_shows_no_poster_cover(self):
        course = self._create_course()
        course.generate_poster()
        module = Module.objects.create(course=course, title='M1')
        lecture = Lecture.objects.create(module=module, title='No Video', is_preview=True)

        response = self.client.get(reverse('course_player', args=[course.id, lecture.id]))
        self.assertNotContains(response, 'id="poster-cover"')


class CourseVersioningTests(TestCase):
    """Editing a published course must resubmit it for review without
    breaking access for students who already paid for it."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='ver_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='ver_stud', password='pw', is_student=True)
        parent_track = Track.objects.create(name='Ver Parent Track')
        track = Track.objects.create(name='Ver Track', parent=parent_track)
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Ver Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED,
        )
        self.module = Module.objects.create(course=self.course, title='M1')
        self.lecture = Lecture.objects.create(module=self.module, title='L1', is_preview=False)
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def _edit(self):
        return self.client.post(reverse('edit_course', args=[self.course.id]), {
            'title': 'Ver Course Updated', 'description': 'updated', 'track': self.course.track_id,
            'level': Course.Level.BEGINNER, 'language': 'English',
            'production_type': Course.ProductionType.FULL, 'price': '0.00', 'is_free': 'on',
        })

    def test_editing_a_published_course_reenters_pending_review(self):
        self.client.force_login(self.instructor)
        self._edit()
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)
        self.assertEqual(self.course.title, 'Ver Course Updated')

    def test_editing_a_published_course_sends_admin_notification(self):
        # Regression test: the pending_course_approvals_count badge picks
        # this up for free (it's a live DB count), but nothing previously
        # sent the actual admin-notification email for a resubmission --
        # only the initial DRAFT/REJECTED -> PENDING_REVIEW submission
        # (course_wizard_review/toggle_publish) did.
        self.client.force_login(self.instructor)
        self._edit()
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Ver Course Updated', notification.subject)
        self.assertIn('Ver Course Updated', notification.body)
        self.assertIn('ver_inst', notification.body)
        self.assertIn(reverse('course_approval_queue'), notification.body)

    def test_enrolled_student_keeps_access_while_edit_is_pending_review(self):
        self.client.force_login(self.instructor)
        self._edit()
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture.id]))
        self.assertEqual(response.status_code, 200)

        detail_response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(detail_response.status_code, 200)

    def test_stranger_cannot_reach_unpublished_course(self):
        self.client.force_login(self.instructor)
        self._edit()

        stranger = User.objects.create_user(username='ver_stranger', password='pw', is_student=True)
        self.client.force_login(stranger)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(response.status_code, 404)

        player_response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture.id]))
        self.assertEqual(player_response.status_code, 404)

    def test_instructor_can_preview_own_unpublished_course(self):
        self.client.force_login(self.instructor)
        self._edit()
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture.id]))
        self.assertEqual(response.status_code, 200)

    def test_editing_a_draft_course_stays_draft(self):
        self.course.status = Course.Status.DRAFT
        self.course.save()
        self.client.force_login(self.instructor)
        self._edit()
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.DRAFT)

    def test_editing_module_on_published_course_reenters_review(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('edit_module', args=[self.course.id, self.module.id]),
                          {'title': 'M1 renamed', 'order': 0})
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

    def test_editing_module_on_published_course_sends_admin_notification(self):
        # Same regression as test_editing_a_published_course_sends_admin_
        # notification above, exercised through a different one of
        # _reenter_review_if_published's ~16 call sites -- confirms the
        # fix lives in the shared helper, not bolted onto edit_course alone.
        self.client.force_login(self.instructor)
        self.client.post(reverse('edit_module', args=[self.course.id, self.module.id]),
                          {'title': 'M1 renamed', 'order': 0})
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Ver Course', notification.subject)


class CourseDeletionTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='del_inst', password='pw', is_instructor=True)
        track = Track.objects.create(name='Del Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Del Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
        )

    def test_course_with_no_history_is_hard_deleted(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('delete_course', args=[self.course.id]))
        self.assertFalse(Course.objects.filter(id=self.course.id).exists())

    def test_course_with_enrollment_is_archived_not_deleted(self):
        student = User.objects.create_user(username='del_stud', password='pw', is_student=True)
        Enrollment.objects.create(student=student, course=self.course)

        self.client.force_login(self.instructor)
        self.client.post(reverse('delete_course', args=[self.course.id]))

        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.ARCHIVED)

    def test_course_with_payment_history_is_archived_not_deleted(self):
        student = User.objects.create_user(username='del_stud2', password='pw', is_student=True)
        Payment.objects.create(student=student, course=self.course, total_amount=Decimal('10.00'))

        self.client.force_login(self.instructor)
        self.client.post(reverse('delete_course', args=[self.course.id]))

        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.ARCHIVED)
        self.assertTrue(Course.objects.filter(id=self.course.id).exists())


@override_settings(STORAGES={
    # File uploads default to Cloudinary in production; swap in an in-memory
    # backend here so this test doesn't attempt a real network call with
    # empty credentials.
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class ProfileAvatarTests(TestCase):
    def test_profile_page_loads_and_shows_initials_without_avatar(self):
        user = User.objects.create_user(username='avatarless', password='pw', is_student=True)
        self.client.force_login(user)
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A')  # initials fallback

    def test_uploading_an_avatar_updates_the_user(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        user = User.objects.create_user(username='avatar_upload', password='pw', is_student=True)
        self.client.force_login(user)
        tiny_gif = (
            b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01'
            b'\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        )
        avatar_file = SimpleUploadedFile('avatar.gif', tiny_gif, content_type='image/gif')
        self.client.post(reverse('profile'), {'avatar': avatar_file})

        user.refresh_from_db()
        self.assertTrue(bool(user.avatar))


class MisfiledCourseDetectionTests(TestCase):
    """The admin dashboard must surface any course that can never appear on
    a student browse page (filed under a parent category, or no track at
    all) so it can be found without a database console."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='misfile_admin', password='pw')
        self.instructor = User.objects.create_user(
            username='misfile_inst', password='pw', is_instructor=True)
        self.parent = Track.objects.create(name='Tech Parent')
        self.leaf = Track.objects.create(name='Web Development Leaf', parent=self.parent)

    def test_course_under_parent_track_is_flagged(self):
        course = Course.objects.create(
            instructor=self.instructor, track=self.parent, title='Misfiled Course',
            description='...', production_type=Course.ProductionType.FULL, price=Decimal('0.00'))
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertContains(response, 'Misfiled Course')
        self.assertIn(course, response.context['misfiled_courses'])

    def test_course_under_leaf_track_is_not_flagged(self):
        Course.objects.create(
            instructor=self.instructor, track=self.leaf, title='Fine Course',
            description='...', production_type=Course.ProductionType.FULL, price=Decimal('0.00'))
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertNotContains(response, 'Fine Course')


class AdminGuardTests(TestCase):
    """Every admin view must be guarded by a real permission check in the view."""

    def setUp(self):
        self.student = User.objects.create_user(username='plain_student', password='pw', is_student=True)
        self.instructor = User.objects.create_user(
            username='plain_instructor', password='pw', is_instructor=True)
        self.admin = User.objects.create_superuser(username='real_admin', password='pw')

    def test_non_admin_cannot_reach_admin_dashboard(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertNotEqual(response.status_code, 200)

    def test_instructor_cannot_reach_admin_pages(self):
        self.client.force_login(self.instructor)
        for name in ('course_approval_queue', 'admin_users', 'admin_payments',
                     'admin_payouts', 'admin_tracks', 'send_test_emails'):
            response = self.client.get(reverse(name))
            self.assertNotEqual(response.status_code, 200, f'{name} was reachable by a non-admin')

    def test_admin_can_reach_admin_pages(self):
        self.client.force_login(self.admin)
        for name in ('admin_dashboard', 'course_approval_queue', 'admin_users', 'admin_payments',
                     'admin_payouts', 'admin_tracks', 'send_test_emails'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, f'{name} was not reachable by an admin')

    def test_anonymous_user_cannot_reach_admin_dashboard(self):
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 302)


class SignupApprovalFlowTests(TestCase):
    def _signup_data(self, **overrides):
        data = {
            'username': 'newbie', 'email': 'newbie@example.com', 'phone_number': '+201001112222',
            'password1': 'a-strong-password-1', 'password2': 'a-strong-password-1',
            'agree_to_terms': 'on',
        }
        data.update(overrides)
        return data

    def _instructor_signup_data(self, **overrides):
        data = self._signup_data(username='newbie_inst', country='Egypt')
        data.update(overrides)
        return data

    def test_student_signup_creates_approved_account(self):
        # Unlike instructors, students carry no revenue-share/payout/
        # course-quality risk, so signup doesn't hold them for manual
        # admin review.
        self.client.post(reverse('student_signup'), self._signup_data())
        user = User.objects.get(username='newbie')
        self.assertTrue(user.is_student)
        self.assertTrue(user.is_approved)

    def test_instructor_signup_creates_unapproved_pending_account(self):
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data())
        user = User.objects.get(username='newbie_inst')
        self.assertTrue(user.is_instructor)
        self.assertFalse(user.is_approved)

    def test_student_signup_auto_logs_in(self):
        # A student account is immediately usable (no approval to wait
        # on), so there's no reason to bounce them to the login form for
        # credentials they just typed in.
        response = self.client.post(reverse('student_signup'), self._signup_data(), follow=True)
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.assertEqual(response.wsgi_request.user.username, 'newbie')

    def test_instructor_signup_does_not_auto_login(self):
        response = self.client.post(reverse('instructor_signup'), self._instructor_signup_data(), follow=True)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_student_signup_redirects_to_homepage_with_welcome_message(self):
        response = self.client.post(reverse('student_signup'), self._signup_data(), follow=True)
        self.assertRedirects(response, reverse('platform_home'))
        self.assertContains(response, 'Welcome to Mendoura')

    def test_instructor_signup_redirects_to_login_with_pending_message(self):
        response = self.client.post(reverse('instructor_signup'), self._instructor_signup_data(), follow=True)
        self.assertRedirects(response, reverse('login'))
        self.assertContains(response, 'pending administrator approval')

    def test_unapproved_user_cannot_login(self):
        # Only instructor signups still produce a pending account -- see
        # test_student_signup_creates_approved_account above.
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data())
        response = self.client.post(
            reverse('login'), {'username': 'newbie_inst', 'password': 'a-strong-password-1'})
        self.assertContains(response, 'Your account is currently pending administrator approval.')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_approved_user_can_login_normally(self):
        user = User.objects.create_user(username='approved_stud', password='pw', is_student=True)
        self.assertTrue(user.is_approved)
        response = self.client.post(reverse('login'), {'username': 'approved_stud', 'password': 'pw'}, follow=True)
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_student_signup_requires_agreeing_to_terms(self):
        data = self._signup_data()
        del data['agree_to_terms']
        self.client.post(reverse('student_signup'), data)
        self.assertFalse(User.objects.filter(username='newbie').exists())

    def test_student_signup_sends_welcome_email(self):
        self.client.post(reverse('student_signup'), self._signup_data())
        sent = next(m for m in mail.outbox if m.to == ['newbie@example.com'])
        self.assertEqual(sent.subject, 'Welcome to Mendoura! 🎉')
        self.assertIn('Ready to get started?', sent.body)
        self.assertTrue(any(content_type == 'text/html' for _, content_type in sent.alternatives))

    def test_student_signup_sends_internal_notification_with_signup_details(self):
        # Same pattern as the instructor-application notification: a new
        # student signup shouldn't sit unnoticed until someone happens to
        # check the admin dashboard.
        self.client.post(reverse('student_signup'), self._signup_data())
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('newbie', notification.subject)
        self.assertIn('newbie', notification.body)
        self.assertIn('newbie@example.com', notification.body)

    def test_instructor_signup_sends_application_received_not_welcome_email(self):
        # The full welcome email fires on approval instead (see
        # AdminUserManagementTests) -- its copy promises dashboard access,
        # which isn't true until an admin approves them. Registration sends
        # the lighter "we got your application" email to the applicant, plus
        # an internal notification to the team (checked separately below).
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data())
        applicant_email = next(m for m in mail.outbox if m.to == ['newbie@example.com'])
        self.assertEqual(applicant_email.subject, "We've received your Mendoura instructor application")

    def test_instructor_signup_sends_internal_notification_with_applicant_details(self):
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data())
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('newbie_inst', notification.subject)
        self.assertIn('newbie_inst', notification.body)
        self.assertIn('newbie@example.com', notification.body)
        self.assertIn(reverse('admin_users'), notification.body)

    def test_signup_without_email_does_not_crash_and_still_notifies_admins(self):
        # The student-facing welcome email has nowhere to go without an
        # email on file, but the internal admin notification (sent to a
        # fixed admin address, not the student's) still fires -- a missing
        # student email must never crash the signup or silently drop the
        # internal notification.
        response = self.client.post(reverse('student_signup'), self._signup_data(email=''))
        self.assertRedirects(response, reverse('platform_home'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])

    def test_student_signup_records_terms_accepted_timestamp(self):
        self.client.post(reverse('student_signup'), self._signup_data())
        user = User.objects.get(username='newbie')
        self.assertIsNotNone(user.terms_accepted_at)

    def test_instructor_signup_requires_agreeing_to_terms(self):
        data = self._instructor_signup_data()
        del data['agree_to_terms']
        self.client.post(reverse('instructor_signup'), data)
        self.assertFalse(User.objects.filter(username='newbie_inst').exists())

    def test_instructor_signup_records_terms_accepted_timestamp(self):
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data())
        user = User.objects.get(username='newbie_inst')
        self.assertIsNotNone(user.terms_accepted_at)

    def test_international_instructor_requires_payoneer_account(self):
        data = self._instructor_signup_data(country='France')
        self.client.post(reverse('instructor_signup'), data)
        self.assertFalse(User.objects.filter(username='newbie_inst').exists())

    def test_international_instructor_with_payoneer_account_succeeds(self):
        data = self._instructor_signup_data(country='France', payoneer_account='inst@payoneer.com')
        self.client.post(reverse('instructor_signup'), data)
        user = User.objects.get(username='newbie_inst')
        self.assertTrue(user.is_international_instructor)
        self.assertEqual(user.payoneer_account, 'inst@payoneer.com')

    def test_egypt_based_instructor_does_not_require_payoneer_account(self):
        self.client.post(reverse('instructor_signup'), self._instructor_signup_data(country='Egypt'))
        user = User.objects.get(username='newbie_inst')
        self.assertFalse(user.is_international_instructor)

    def test_admin_cannot_approve_international_instructor_without_payoneer(self):
        admin = User.objects.create_superuser(username='legal_admin', password='pw')
        self.client.post(reverse('instructor_signup'),
                          self._instructor_signup_data(country='Germany', payoneer_account='temp@payoneer.com'))
        user = User.objects.get(username='newbie_inst')
        user.payoneer_account = ''
        user.save()

        self.client.force_login(admin)
        self.client.post(reverse('approve_user', args=[user.id]))
        user.refresh_from_db()
        self.assertFalse(user.is_approved)


class AdminUserManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='mgmt_admin', password='pw')
        self.pending = User.objects.create_user(
            username='pending_stud', password='pw', is_student=True, is_approved=False)
        self.student = User.objects.create_user(
            username='active_stud', password='pw', is_student=True)
        self.instructor = User.objects.create_user(
            username='active_inst', password='pw', is_instructor=True)

    def test_admin_users_page_lists_pending_students_and_instructors_separately(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_users'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'pending_stud')
        self.assertContains(response, 'active_stud')
        self.assertContains(response, 'active_inst')

    def test_admin_can_approve_pending_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('approve_user', args=[self.pending.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.is_approved)

    def test_approving_student_does_not_send_instructor_welcome_email(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('approve_user', args=[self.pending.id]))
        self.assertEqual(len(mail.outbox), 0)

    def test_approving_instructor_sends_instructor_welcome_email(self):
        pending_instructor = User.objects.create_user(
            username='pending_inst', password='pw', is_instructor=True, is_approved=False,
            email='pending_inst@example.com', country='Egypt', first_name='Ivy', last_name='Instructor')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('approve_user', args=[pending_instructor.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "Welcome to Mendoura — Let's build your first course 🎓")
        self.assertEqual(sent.to, ['pending_inst@example.com'])
        self.assertIn('instructor dashboard', sent.body)
        self.assertIn(reverse('instructor_dashboard'), sent.body)

    def test_instructor_signup_sends_application_received_not_full_welcome_email(self):
        """The full Instructor welcome email fires on approval (see
        test_approving_instructor_sends_instructor_welcome_email above),
        not at registration -- its copy promises dashboard access, which
        isn't true until an admin approves the account. Registration gets
        the lighter application-received email instead."""
        response = self.client.post(reverse('instructor_signup'), {
            'username': 'freshinst', 'email': 'freshinst@example.com', 'phone_number': '+201009998888',
            'country': 'Egypt', 'password1': 'a-strong-password-1', 'password2': 'a-strong-password-1',
            'agree_to_terms': 'on',
        })
        self.assertRedirects(response, reverse('login'))
        applicant_email = next(m for m in mail.outbox if m.to == ['freshinst@example.com'])
        self.assertEqual(applicant_email.subject, "We've received your Mendoura instructor application")

    def test_admin_can_reject_pending_user_and_it_is_deleted(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('reject_user', args=[self.pending.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertFalse(User.objects.filter(id=self.pending.id).exists())

    def test_reject_does_not_affect_already_approved_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('reject_user', args=[self.student.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(User.objects.filter(id=self.student.id).exists())

    def test_rejecting_pending_student_does_not_send_an_email(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('reject_user', args=[self.pending.id]))
        self.assertEqual(len(mail.outbox), 0)

    def test_rejecting_pending_instructor_sends_rejection_email(self):
        pending_instructor = User.objects.create_user(
            username='rejected_inst', password='pw', is_instructor=True, is_approved=False,
            email='rejected_inst@example.com', country='Egypt', first_name='Rex', last_name='Rejected')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('reject_user', args=[pending_instructor.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertFalse(User.objects.filter(id=pending_instructor.id).exists())
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, 'An update on your Mendoura instructor application')
        self.assertEqual(sent.to, ['rejected_inst@example.com'])

    def test_pending_instructor_count_badge_shown_to_admin(self):
        User.objects.create_user(
            username='badge_inst', password='pw', is_instructor=True, is_approved=False,
            email='badge_inst@example.com')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'pending instructor request')

    def test_pending_instructor_count_badge_hidden_for_non_admin(self):
        User.objects.create_user(
            username='badge_inst2', password='pw', is_instructor=True, is_approved=False,
            email='badge_inst2@example.com')
        self.client.force_login(self.student)
        response = self.client.get(reverse('platform_home'))
        self.assertNotContains(response, 'pending instructor request')

    def test_pending_instructor_count_clears_after_approval(self):
        pending_instructor = User.objects.create_user(
            username='badge_inst3', password='pw', is_instructor=True, is_approved=False,
            email='badge_inst3@example.com', country='Egypt')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_instructor_requests_count'], 1)
        self.client.post(reverse('approve_user', args=[pending_instructor.id]))
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_instructor_requests_count'], 0)

    def test_admin_can_delete_active_student(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('delete_user', args=[self.student.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertFalse(User.objects.filter(id=self.student.id).exists())

    def test_admin_can_delete_active_instructor(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('delete_user', args=[self.instructor.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertFalse(User.objects.filter(id=self.instructor.id).exists())

    def test_admin_cannot_delete_own_account(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('delete_user', args=[self.admin.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertTrue(User.objects.filter(id=self.admin.id).exists())

    def test_admin_cannot_delete_another_superuser(self):
        other_admin = User.objects.create_superuser(username='other_admin', password='pw')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('delete_user', args=[other_admin.id]))
        self.assertRedirects(response, reverse('admin_users'))
        self.assertTrue(User.objects.filter(id=other_admin.id).exists())

    def test_delete_user_with_protected_history_shows_error_instead_of_crashing(self):
        track = Track.objects.create(name='Delete-Protected Track')
        course = Course.objects.create(
            instructor=self.instructor, track=track, title='Protected Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('10.00'))
        Payment.objects.create(student=self.student, course=course, total_amount=Decimal('10.00'))

        self.client.force_login(self.admin)
        response = self.client.post(reverse('delete_user', args=[self.student.id]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(id=self.student.id).exists())
        self.assertContains(response, 'has payment or revenue history')

    def test_non_admin_cannot_approve_reject_or_delete_users(self):
        self.client.force_login(self.student)
        for name, args in (
            ('approve_user', [self.pending.id]),
            ('reject_user', [self.pending.id]),
            ('delete_user', [self.instructor.id]),
        ):
            response = self.client.post(reverse(name, args=args))
            self.assertNotEqual(response.status_code, 200)
        self.assertFalse(User.objects.get(id=self.pending.id).is_approved)
        self.assertTrue(User.objects.filter(id=self.instructor.id).exists())


class CourseApprovalTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='approver', password='pw')
        self.instructor = User.objects.create_user(
            username='pending_inst', password='pw', is_instructor=True, email='pending_inst@example.com')
        track = Track.objects.create(name='UI/UX Design')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Pending Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PENDING_REVIEW,
        )

    def test_approve_publishes_course(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('approve_course', args=[self.course.id]))
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PUBLISHED)

    def test_approve_sends_course_approved_email(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('approve_course', args=[self.course.id]))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['pending_inst@example.com'])
        self.assertIn('Pending Course', sent.subject)
        self.assertIn('Pending Course', sent.body)

    def test_reject_stores_reason(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('reject_course', args=[self.course.id]), {'reason': 'Low quality audio'})
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.REJECTED)
        self.assertEqual(self.course.rejection_reason, 'Low quality audio')

    def test_reject_with_reason_sends_email_containing_reason(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('reject_course', args=[self.course.id]), {'reason': 'Low quality audio'})
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['pending_inst@example.com'])
        self.assertIn('Pending Course', sent.subject)
        self.assertIn('Low quality audio', sent.body)

    def test_reject_without_reason_is_rejected_and_sends_nothing(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('reject_course', args=[self.course.id]), {'reason': ''}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please enter a rejection reason')
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)
        self.assertEqual(len(mail.outbox), 0)

    def test_reject_with_whitespace_only_reason_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('reject_course', args=[self.course.id]), {'reason': '   '}, follow=True)
        self.assertContains(response, 'Please enter a rejection reason')
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

    def test_instructor_cannot_approve_own_course(self):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('approve_course', args=[self.course.id]))
        self.assertNotEqual(response.status_code, 200)
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

    def test_rejecting_one_course_does_not_touch_others(self):
        # Regression test: rejecting a single course must never affect any
        # other course -- not its status, and definitely not deleting it.
        track = Track.objects.create(name='Reject Isolation Track')
        course_b = Course.objects.create(
            instructor=self.instructor, track=track, title='Course B', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PENDING_REVIEW,
        )
        course_c = Course.objects.create(
            instructor=self.instructor, track=track, title='Course C', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PENDING_REVIEW,
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('reject_course', args=[self.course.id]), {'reason': 'Not good enough'})
        self.assertEqual(response.status_code, 302)

        self.course.refresh_from_db()
        course_b.refresh_from_db()
        course_c.refresh_from_db()

        self.assertEqual(self.course.status, Course.Status.REJECTED)
        self.assertEqual(course_b.status, Course.Status.PENDING_REVIEW)
        self.assertEqual(course_c.status, Course.Status.PENDING_REVIEW)
        self.assertEqual(Course.objects.count(), 3)  # nothing deleted

    def test_toggle_publish_sends_course_submission_notification(self):
        # Same pattern as the instructor-application notification: the
        # classic dashboard's "Submit for Review" button (not just the
        # wizard's) must notify admins too.
        track = Track.objects.create(name='Toggle Publish Track')
        draft_course = Course.objects.create(
            instructor=self.instructor, track=track, title='Draft Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.DRAFT,
        )
        self.client.force_login(self.instructor)
        self.client.post(reverse('toggle_publish', args=[draft_course.id]))
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Draft Course', notification.subject)
        self.assertIn('Draft Course', notification.body)
        self.assertIn('pending_inst', notification.body)
        self.assertIn(reverse('course_approval_queue'), notification.body)

    def test_pending_course_approvals_count_badge_shown_to_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'pending course approval')

    def test_pending_course_approvals_count_badge_hidden_for_non_admin(self):
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('platform_home'))
        self.assertNotContains(response, 'pending course approval')

    def test_pending_course_approvals_count_clears_after_approval(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_course_approvals_count'], 1)
        self.client.post(reverse('approve_course', args=[self.course.id]))
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_course_approvals_count'], 0)


class TrackRequestTests(TestCase):
    """New-track requests: an instructor-facing counterpart to Course's
    submit-for-review flow, approved/rejected the same way (see
    TrackRequest's docstring in models.py for why it's a separate model
    rather than a status on Track itself)."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='track_approver', password='pw')
        self.instructor = User.objects.create_user(
            username='track_req_inst', password='pw', is_instructor=True,
            email='track_req_inst@example.com')
        self.other_instructor = User.objects.create_user(
            username='track_req_other', password='pw', is_instructor=True)
        self.category = Track.objects.create(name='Tech')

    def test_instructor_can_submit_a_track_request(self):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('request_track'), {
            'parent': self.category.id, 'name': 'Robotics', 'reason': 'Growing demand.',
        })
        self.assertRedirects(response, reverse('request_track'))
        track_request = TrackRequest.objects.get(name='Robotics')
        self.assertEqual(track_request.instructor, self.instructor)
        self.assertEqual(track_request.parent, self.category)
        self.assertEqual(track_request.status, TrackRequest.Status.PENDING)
        self.assertIsNone(track_request.track)
        # Not yet a real Track -- the whole point of the separate model.
        self.assertFalse(Track.objects.filter(name='Robotics').exists())

    def test_submission_sends_admin_notification(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('request_track'), {
            'parent': self.category.id, 'name': 'Robotics', 'reason': 'Growing demand.',
        })
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Robotics', notification.subject)
        self.assertIn('Robotics', notification.body)
        self.assertIn('Tech', notification.body)
        self.assertIn('track_req_inst', notification.body)
        self.assertIn(reverse('track_approval_queue'), notification.body)

    def test_instructor_sees_own_requests_with_status(self):
        TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('request_track'))
        self.assertContains(response, 'Robotics')
        self.assertContains(response, 'Pending Review')

    def test_non_instructor_cannot_submit_a_track_request(self):
        student = User.objects.create_user(username='track_req_stud', password='pw', is_student=True)
        self.client.force_login(student)
        response = self.client.post(reverse('request_track'), {
            'parent': self.category.id, 'name': 'Robotics',
        })
        self.assertRedirects(response, reverse('platform_home'))
        self.assertFalse(TrackRequest.objects.filter(name='Robotics').exists())

    def test_admin_approve_creates_real_track_and_sends_email(self):
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('approve_track_request', args=[track_request.id]))
        self.assertRedirects(response, reverse('track_approval_queue'))

        track_request.refresh_from_db()
        self.assertEqual(track_request.status, TrackRequest.Status.APPROVED)
        self.assertIsNotNone(track_request.track)
        self.assertEqual(track_request.track.name, 'Robotics')
        self.assertEqual(track_request.track.parent, self.category)
        self.assertTrue(track_request.track.is_active)

        sent = next(m for m in mail.outbox if m.to == ['track_req_inst@example.com'])
        self.assertIn('Robotics', sent.subject)
        self.assertIn('Robotics', sent.body)

    def test_approved_track_is_selectable_in_course_creation(self):
        from .forms import CourseCreationForm
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        self.client.post(reverse('approve_track_request', args=[track_request.id]))

        form = CourseCreationForm()
        self.assertIn(Track.objects.get(name='Robotics'), form.fields['track'].queryset)

    def test_admin_reject_requires_reason_and_sends_no_track(self):
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('reject_track_request', args=[track_request.id]), {'reason': ''}, follow=True)
        self.assertContains(response, 'Please enter a rejection reason')
        track_request.refresh_from_db()
        self.assertEqual(track_request.status, TrackRequest.Status.PENDING)
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_reject_with_reason_sends_email_containing_reason_and_creates_no_track(self):
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('reject_track_request', args=[track_request.id]),
            {'reason': 'Too narrow, fold into AI & Machine Learning instead.'})
        self.assertRedirects(response, reverse('track_approval_queue'))

        track_request.refresh_from_db()
        self.assertEqual(track_request.status, TrackRequest.Status.REJECTED)
        self.assertEqual(track_request.rejection_reason, 'Too narrow, fold into AI & Machine Learning instead.')
        self.assertIsNone(track_request.track)
        self.assertFalse(Track.objects.filter(name='Robotics').exists())

        sent = next(m for m in mail.outbox if m.to == ['track_req_inst@example.com'])
        self.assertIn('Robotics', sent.subject)
        self.assertIn('Too narrow, fold into AI & Machine Learning instead.', sent.body)

    def test_approving_name_collision_shows_error_instead_of_crashing(self):
        Track.objects.create(parent=self.category, name='Robotics')
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('approve_track_request', args=[track_request.id]), follow=True)
        self.assertContains(response, 'already exists')
        track_request.refresh_from_db()
        self.assertEqual(track_request.status, TrackRequest.Status.PENDING)

    def test_instructor_cannot_access_track_approval_queue(self):
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('track_approval_queue'))
        self.assertNotEqual(response.status_code, 200)

    def test_pending_track_requests_count_badge_shown_to_admin(self):
        TrackRequest.objects.create(instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'pending track request')

    def test_pending_track_requests_count_badge_hidden_for_non_admin(self):
        TrackRequest.objects.create(instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('platform_home'))
        self.assertNotContains(response, 'pending track request')

    def test_pending_track_requests_count_clears_after_approval(self):
        track_request = TrackRequest.objects.create(
            instructor=self.instructor, parent=self.category, name='Robotics')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_track_requests_count'], 1)
        self.client.post(reverse('approve_track_request', args=[track_request.id]))
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.context['pending_track_requests_count'], 0)


class AdminCoursePreviewTests(TestCase):
    """The Course Approval Queue only ever showed metadata (title, price,
    category) -- no way to actually review the video/quiz content before
    approving or rejecting. course_detail() already had an admin/owner
    bypass for the not-yet-published 404 gate; this extends it so an admin
    can also open every lecture (not just ones marked "free preview") and
    see quiz questions/answers directly on the page."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='preview_admin', password='pw')
        self.instructor = User.objects.create_user(
            username='preview_inst', password='pw', is_instructor=True)
        self.other_student = User.objects.create_user(
            username='preview_outsider', password='pw', is_student=True)
        track = Track.objects.create(name='Preview Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Preview Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PENDING_REVIEW,
        )
        self.module = Module.objects.create(course=self.course, title='M1')
        self.non_preview_lecture = Lecture.objects.create(
            module=self.module, title='Non-preview lecture', is_preview=False)
        self.quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.question = Question.objects.create(quiz=self.quiz, text='2+2=?', order=1)
        Choice.objects.create(question=self.question, text='4', is_correct=True)
        Choice.objects.create(question=self.question, text='5', is_correct=False)

    def test_approval_queue_links_to_course_detail_preview(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('course_approval_queue'))
        self.assertContains(response, reverse('course_detail', args=[self.course.id]))

    def test_admin_can_open_course_detail_for_pending_course(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(response.status_code, 200)

    def test_admin_sees_watch_link_for_non_preview_lecture(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertContains(response, reverse('course_player', args=[self.course.id, self.non_preview_lecture.id]))

    def test_admin_sees_quiz_questions_and_correct_answer(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertContains(response, '2+2=?')
        self.assertContains(response, '4')
        self.assertContains(response, '5')

    def test_outsider_student_cannot_open_pending_course_detail(self):
        self.client.force_login(self.other_student)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(response.status_code, 404)

    def test_outsider_student_does_not_see_watch_link_for_non_preview_lecture(self):
        # Sanity check for a published course: a random student (no
        # enrollment) still only gets a Watch/Preview link for lectures
        # actually marked is_preview -- the admin bypass must not leak to
        # everyone.
        self.course.status = Course.Status.PUBLISHED
        self.course.save()
        self.client.force_login(self.other_student)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, reverse('course_player', args=[self.course.id, self.non_preview_lecture.id]))

    def test_admin_can_watch_non_preview_lecture_video(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.non_preview_lecture.id]))
        self.assertEqual(response.status_code, 200)


class EnrolledStudentCourseContentTests(TestCase):
    """Regression coverage for the "enrolled student has no way to actually
    watch a course" bug: course_detail's Course Content list only linked a
    lecture to course_player when it was marked is_preview or the viewer
    could preview_all (owner/admin) -- an enrolled-but-not-owner student
    fell through both branches and got neither a link nor a lock icon,
    i.e. nothing clickable at all. course_player itself (the actual
    Bunny-embed player) was already correctly gated and needed no changes;
    only the missing link in detail.html did."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='enrolled_content_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='enrolled_content_stud', password='pw', is_student=True)
        self.outsider = User.objects.create_user(
            username='enrolled_content_outsider', password='pw', is_student=True)
        track = Track.objects.create(name='Enrolled Content Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Enrolled Content Course',
            description='...', production_type=Course.ProductionType.FULL,
            price=Decimal('0.00'), is_free=True, status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=self.course, title='M1')
        self.locked_lecture = Lecture.objects.create(module=module, title='Deep Dive', is_preview=False)
        Enrollment.objects.create(student=self.student, course=self.course)

    def test_enrolled_student_sees_watch_link_for_non_preview_lecture(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertContains(
            response, reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertContains(response, 'Watch')

    def test_enrolled_student_lecture_title_itself_is_a_link(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertContains(
            response,
            f'href="{reverse("course_player", args=[self.course.id, self.locked_lecture.id])}"',
            count=2)  # the title link and the "Watch" pill

    def test_enrolled_student_can_actually_open_the_player(self):
        self.client.force_login(self.student)
        response = self.client.get(
            reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertEqual(response.status_code, 200)

    def test_non_enrolled_student_sees_lock_not_a_link(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertNotContains(
            response, reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertContains(response, 'fa-lock')

    def test_logged_out_visitor_sees_lock_not_a_link(self):
        response = self.client.get(reverse('course_detail', args=[self.course.id]))
        self.assertNotContains(
            response, reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertContains(response, 'fa-lock')


class AdminPayoutLifecycleTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='payout_admin', password='pw')
        self.instructor = User.objects.create_user(
            username='payout_recipient', password='pw', is_instructor=True)
        self.wallet = InstructorWallet.objects.create(
            instructor=self.instructor, available_balance=Decimal('0.00'),
            pending_balance=Decimal('30.00'))
        self.payout = Payout.objects.create(wallet=self.wallet, amount=Decimal('30.00'), method='bank')

    def test_approve_then_mark_paid_moves_pending_to_withdrawn(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('approve_payout', args=[self.payout.id]))
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, Payout.Status.APPROVED)

        self.client.post(reverse('mark_payout_paid', args=[self.payout.id]))
        self.payout.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.payout.status, Payout.Status.PAID)
        self.assertEqual(self.wallet.pending_balance, Decimal('0.00'))
        self.assertEqual(self.wallet.total_withdrawn, Decimal('30.00'))
        self.assertTrue(WalletTransaction.objects.filter(
            wallet=self.wallet, type=WalletTransaction.Type.WITHDRAWAL, amount=Decimal('30.00')).exists())

    def test_reject_returns_funds_to_available_balance(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('reject_payout', args=[self.payout.id]))
        self.payout.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.payout.status, Payout.Status.REJECTED)
        self.assertEqual(self.wallet.pending_balance, Decimal('0.00'))
        self.assertEqual(self.wallet.available_balance, Decimal('30.00'))


class TrackCrudTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='crud_admin', password='pw')

    def test_admin_can_create_and_deactivate_track(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_tracks'), {'name': 'Robotics', 'description': '', 'icon': '', 'order': 0})
        track = Track.objects.get(name='Robotics')
        self.assertTrue(track.is_active)

        self.client.post(reverse('toggle_track_active', args=[track.id]))
        track.refresh_from_db()
        self.assertFalse(track.is_active)


class TrackTranslationTests(TestCase):
    """Track.save() auto-translates name/description via the AI API. The
    network call itself (auto_translate.translate_fields) is mocked -- these
    tests are about the save()-time trigger/staleness/fallback logic, not
    the Anthropic integration (that's covered by mocking, same as
    AICoachTests)."""

    def test_without_ai_configured_save_succeeds_and_falls_back_to_source(self):
        with override_settings(AUTO_TRANSLATE_ENABLED=False):
            track = Track.objects.create(name='Robotics', description='Build robots.')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')
        self.assertEqual(track.translated_description, 'Build robots.')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_save_populates_translations_for_every_active_language(self, mock_translate):
        mock_translate.return_value = {
            'name': {'ar': 'الروبوتات', 'fr': 'Robotique', 'es': 'Robótica'},
            'description': {'ar': 'ابنِ روبوتات.', 'fr': 'Construisez des robots.', 'es': 'Construye robots.'},
        }
        track = Track.objects.create(name='Robotics', description='Build robots.')

        mock_translate.assert_called_once()
        fields_arg, target_languages_arg = mock_translate.call_args.args
        self.assertEqual(fields_arg, {'name': 'Robotics', 'description': 'Build robots.'})
        self.assertEqual(set(target_languages_arg), {'ar', 'fr', 'es'})

        self.assertEqual(track.name_translations['ar'], 'الروبوتات')
        self.assertEqual(track.name_translations['fr'], 'Robotique')
        self.assertEqual(track.name_translations['es'], 'Robótica')
        self.assertEqual(track.description_translations['ar'], 'ابنِ روبوتات.')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_translated_name_resolves_active_language_and_falls_back_to_english(self, mock_translate):
        mock_translate.return_value = {
            'name': {'ar': 'الروبوتات', 'fr': 'Robotique', 'es': 'Robótica'},
            'description': {'ar': '', 'fr': '', 'es': ''},
        }
        track = Track.objects.create(name='Robotics', description='')

        with translation_override('ar'):
            self.assertEqual(track.translated_name, 'الروبوتات')
        with translation_override('fr'):
            self.assertEqual(track.translated_name, 'Robotique')
        with translation_override('en'):
            self.assertEqual(track.translated_name, 'Robotics')
        # A language with no translation available (or none active) falls back too.
        with translation_override('de'):
            self.assertEqual(track.translated_name, 'Robotics')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_unchanged_name_does_not_retrigger_translation_on_next_save(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'أ', 'fr': 'f', 'es': 'e'}, 'description': {}}
        track = Track.objects.create(name='Robotics', description='')
        self.assertEqual(mock_translate.call_count, 1)

        track.order = 5
        track.save()
        self.assertEqual(mock_translate.call_count, 1)

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_changed_name_retriggers_translation(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'أ', 'fr': 'f', 'es': 'e'}, 'description': {}}
        track = Track.objects.create(name='Robotics', description='')
        self.assertEqual(mock_translate.call_count, 1)

        mock_translate.return_value = {'name': {'ar': 'ب', 'fr': 'g', 'es': 'h'}, 'description': {}}
        track.name = 'Advanced Robotics'
        track.save()
        self.assertEqual(mock_translate.call_count, 2)
        self.assertEqual(track.name_translations['ar'], 'ب')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_translation_error_does_not_break_save(self, mock_translate):
        mock_translate.side_effect = auto_translate.TranslationError('boom')
        track = Track.objects.create(name='Robotics', description='Build robots.')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')

    @override_settings(AUTO_TRANSLATE_ENABLED=False)
    def test_local_fallback_translates_known_track_name_without_ai(self):
        track = Track.objects.create(name='Cybersecurity')
        self.assertEqual(track.name_translations['ar'], 'الأمن السيبراني')
        with translation_override('ar'):
            self.assertEqual(track.translated_name, 'الأمن السيبراني')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_local_fallback_fills_gap_when_ai_call_fails(self, mock_translate):
        mock_translate.side_effect = auto_translate.TranslationError('boom')
        track = Track.objects.create(name='Web Development')
        self.assertEqual(track.name_translations['ar'], 'تطوير الويب')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    @patch('courses.models.auto_translate.translate_fields')
    def test_real_ai_result_takes_priority_over_local_fallback(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'ترجمة حقيقية', 'fr': 'f', 'es': 'e'}}
        track = Track.objects.create(name='Tech')
        # The AI's own Arabic translation wins over the local dictionary entry.
        self.assertEqual(track.name_translations['ar'], 'ترجمة حقيقية')

    @override_settings(AUTO_TRANSLATE_ENABLED=False)
    def test_unmapped_track_name_still_falls_back_to_english_without_ai(self):
        track = Track.objects.create(name='Robotics')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')

    @override_settings(AUTO_TRANSLATE_ENABLED=False)
    def test_local_fallback_translates_languages_track_name_without_ai(self):
        track = Track.objects.create(name='Languages')
        self.assertEqual(track.name_translations['ar'], 'اللغات')
        with translation_override('ar'):
            self.assertEqual(track.translated_name, 'اللغات')

    @override_settings(AUTO_TRANSLATE_ENABLED=False)
    def test_local_fallback_only_covers_arabic_not_french_or_spanish(self):
        track = Track.objects.create(name='Marketing')
        self.assertEqual(track.name_translations, {'ar': 'تسويق', '__source__': 'Marketing'})
        with translation_override('fr'):
            self.assertEqual(track.translated_name, 'Marketing')


TEST_HMAC_SECRET = 'test-hmac-secret'


def _signed_transaction(merchant_order_id, **overrides):
    """Build a Paymob transaction dict shaped like the real callback payload
    (order/owner as nested objects) plus the correct HMAC for it, computed the
    same way the webhook view does: flatten, then sign."""
    nested = {
        'amount_cents': 5000, 'created_at': '2026-01-01T00:00:00Z', 'currency': 'EGP',
        'error_occured': False, 'has_parent_transaction': False, 'id': 123456,
        'integration_id': 1, 'is_3d_secure': True, 'is_auth': False, 'is_capture': False,
        'is_refunded': False, 'is_standalone_payment': True, 'is_voided': False,
        'order': {'id': 999, 'merchant_order_id': merchant_order_id}, 'owner': {'id': 1},
        'pending': False,
        'source_data': {'pan': '1234', 'sub_type': 'VISA', 'type': 'card'},
        'success': True,
    }
    nested.update(overrides)
    flat = paymob.flatten_callback_obj(nested)
    concatenated = ''.join(str(flat.get(f, '')) for f in paymob.HMAC_FIELDS)
    signature = hmac.new(TEST_HMAC_SECRET.encode(), concatenated.encode(), hashlib.sha512).hexdigest()
    return nested, signature


@override_settings(PAYMOB_HMAC_SECRET=TEST_HMAC_SECRET)
class PaymobHmacTests(TestCase):
    def test_valid_signature_verifies(self):
        nested, signature = _signed_transaction('course1-student2-abc123')
        self.assertTrue(paymob.verify_hmac(paymob.flatten_callback_obj(nested), signature))

    def test_tampered_amount_fails_verification(self):
        nested, signature = _signed_transaction('course1-student2-abc123')
        nested['amount_cents'] = 999999  # attacker changes the amount after signing
        self.assertFalse(paymob.verify_hmac(paymob.flatten_callback_obj(nested), signature))

    def test_wrong_signature_fails_verification(self):
        nested, _ = _signed_transaction('course1-student2-abc123')
        self.assertFalse(
            paymob.verify_hmac(paymob.flatten_callback_obj(nested), 'not-the-real-signature'))

    def test_flatten_callback_obj_extracts_nested_ids(self):
        raw = {
            'order': {'id': 999, 'merchant_order_id': 'course1-student2-abcdef1234'},
            'owner': {'id': 1},
            'source_data': {'pan': '1234', 'sub_type': 'VISA', 'type': 'card'},
        }
        flat = paymob.flatten_callback_obj(raw)
        self.assertEqual(flat['order'], 999)
        self.assertEqual(flat['owner'], 1)
        self.assertEqual(flat['source_data_pan'], '1234')


@override_settings(PAYMOB_HMAC_SECRET=TEST_HMAC_SECRET)
class PaymobWebhookTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='paymob_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='paymob_stud', password='pw', is_student=True,
            email='paymob_stud@example.com')
        track = Track.objects.create(name='Business & Marketing')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Paid Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('50.00'),
            status=Course.Status.PUBLISHED,
        )

    def _post_webhook(self, nested, signature):
        return self.client.post(
            f"{reverse('paymob_webhook')}?hmac={signature}",
            data=json.dumps({'obj': nested}), content_type='application/json')

    def test_successful_transaction_creates_payment_enrollment_and_wallet_credit(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)

        response = self._post_webhook(nested, signature)
        self.assertEqual(response.status_code, 200)

        payment = Payment.objects.get(provider_transaction_id='123456')
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(payment.instructor_amount, Decimal('35.00'))
        self.assertTrue(Enrollment.objects.filter(student=self.student, course=self.course).exists())

        wallet = InstructorWallet.objects.get(instructor=self.instructor)
        self.assertEqual(wallet.available_balance, Decimal('35.00'))
        self.assertTrue(WalletTransaction.objects.filter(
            wallet=wallet, type=WalletTransaction.Type.SALE_CREDIT, amount=Decimal('35.00')).exists())

    def test_successful_purchase_sends_admin_notification(self):
        # Same pattern as the instructor-application notification: sent
        # from inside the same `if created:` guard the wallet credit
        # happens in, so a retried webhook delivery can't double-send it.
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)
        self._post_webhook(nested, signature)
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Paid Course', notification.subject)
        self.assertIn('Paid Course', notification.body)
        self.assertIn('paymob_stud', notification.body)
        self.assertIn('50 USD', notification.body)

    def test_successful_purchase_sends_enrollment_confirmation_to_student(self):
        # Regression test: a paid enrollment (unlike the free-course path)
        # only ever sent the internal admin notification -- the student
        # who actually paid got nothing.
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)
        self._post_webhook(nested, signature)
        sent = next(m for m in mail.outbox if m.to == ['paymob_stud@example.com'])
        self.assertIn('Paid Course', sent.subject)
        self.assertIn('Paid Course', sent.body)

    def test_duplicate_purchase_webhook_does_not_double_notify(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)
        self._post_webhook(nested, signature)
        self._post_webhook(nested, signature)  # Paymob retries the same delivery
        notifications = [
            m for m in mail.outbox if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL]]
        self.assertEqual(len(notifications), 1)

    def test_duplicate_webhook_delivery_does_not_double_credit(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)

        self._post_webhook(nested, signature)
        self._post_webhook(nested, signature)  # Paymob retries the same delivery

        self.assertEqual(Payment.objects.filter(provider_transaction_id='123456').count(), 1)
        wallet = InstructorWallet.objects.get(instructor=self.instructor)
        self.assertEqual(wallet.available_balance, Decimal('35.00'))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 1)

    def test_invalid_signature_is_rejected(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, _ = _signed_transaction(merchant_order_id)
        response = self._post_webhook(nested, 'totally-fake-signature')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Payment.objects.exists())

    def test_failed_transaction_creates_nothing(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id, success=False)

        response = self._post_webhook(nested, signature)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.exists())

    def test_refund_reverses_wallet_credit(self):
        merchant_order_id = f'course{self.course.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id)
        self._post_webhook(nested, signature)

        wallet = InstructorWallet.objects.get(instructor=self.instructor)
        self.assertEqual(wallet.available_balance, Decimal('35.00'))

        refund_nested, refund_signature = _signed_transaction(merchant_order_id, is_refunded=True)
        response = self._post_webhook(refund_nested, refund_signature)
        self.assertEqual(response.status_code, 200)

        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal('0.00'))
        payment = Payment.objects.get(provider_transaction_id='123456')
        self.assertEqual(payment.status, Payment.Status.REFUNDED)
        self.assertTrue(WalletTransaction.objects.filter(
            wallet=wallet, type=WalletTransaction.Type.REFUND_DEBIT, amount=Decimal('35.00')).exists())


@override_settings(PAYMOB_HMAC_SECRET=TEST_HMAC_SECRET)
class SubscriptionWebhookTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username='sub_stud', password='pw', is_student=True)
        self.plan = Plan.objects.create(
            name='Mendoura Annual Pass', price_egp=Decimal('1499.00'), price_usd=Decimal('49.00'))

    def _post_webhook(self, nested, signature):
        return self.client.post(
            f"{reverse('paymob_webhook')}?hmac={signature}",
            data=json.dumps({'obj': nested}), content_type='application/json')

    def test_successful_subscription_payment_creates_active_subscription(self):
        merchant_order_id = f'sub{self.plan.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id, amount_cents=149900)

        response = self._post_webhook(nested, signature)
        self.assertEqual(response.status_code, 200)

        subscription = Subscription.objects.get(provider_transaction_id='123456')
        self.assertEqual(subscription.student, self.student)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.amount_paid, Decimal('1499.00'))
        self.assertTrue(subscription.is_active_now())

    def test_duplicate_subscription_webhook_does_not_double_create(self):
        merchant_order_id = f'sub{self.plan.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id, amount_cents=149900)

        self._post_webhook(nested, signature)
        self._post_webhook(nested, signature)

        self.assertEqual(Subscription.objects.filter(provider_transaction_id='123456').count(), 1)

    def test_successful_subscription_sends_admin_notification(self):
        # Same pattern as the instructor-application notification: sent
        # from inside the same `if created:` guard the SubscriptionPeriod
        # row is created in, so a retried webhook delivery can't double-
        # send it.
        merchant_order_id = f'sub{self.plan.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id, amount_cents=149900)
        self._post_webhook(nested, signature)
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('sub_stud', notification.body)
        self.assertIn('Mendoura Annual Pass', notification.body)
        self.assertIn('Annual', notification.body)
        self.assertIn('1499 EGP', notification.body)

    def test_duplicate_subscription_webhook_does_not_double_notify(self):
        merchant_order_id = f'sub{self.plan.id}-student{self.student.id}-abc123'
        nested, signature = _signed_transaction(merchant_order_id, amount_cents=149900)
        self._post_webhook(nested, signature)
        self._post_webhook(nested, signature)
        notifications = [
            m for m in mail.outbox if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL]]
        self.assertEqual(len(notifications), 1)


class SubscriptionAccessControlTests(TestCase):
    """An active subscriber gets frictionless access to any paid course
    without an individual purchase."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='sub_access_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='sub_access_stud', password='pw', is_student=True,
            email='sub_access_stud@example.com')
        track = Track.objects.create(name='Cloud & DevOps')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Paid Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('40.00'),
            status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=self.course, title='Module 1')
        self.locked_lecture = Lecture.objects.create(module=module, title='Deep Dive', is_preview=False)
        plan = Plan.objects.create(name='Mendoura Annual Pass', price_egp=Decimal('1499.00'),
                                    price_usd=Decimal('49.00'))
        self.subscription = Subscription.objects.create(
            student=self.student, plan=plan, amount_paid=Decimal('1499.00'),
            expires_at=timezone.now() + timedelta(days=365),
        )

    def test_active_subscriber_can_watch_locked_lecture_without_buying_course(self):
        self.client.force_login(self.student)
        response = self.client.get(
            reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Enrollment.objects.filter(
            student=self.student, course=self.course, via_subscription=True).exists())

    def test_expired_subscriber_cannot_watch_locked_lecture(self):
        self.subscription.expires_at = timezone.now() - timedelta(days=1)
        self.subscription.save()
        self.client.force_login(self.student)
        response = self.client.get(
            reverse('course_player', args=[self.course.id, self.locked_lecture.id]))
        self.assertEqual(response.status_code, 403)

    def test_subscriber_clicking_enroll_gets_confirmation_email(self):
        # The explicit "Enroll"/"Buy Now" click (enroll_course's instant-
        # unlock branch for an active subscriber) is a deliberate action and
        # should be confirmed by email -- unlike course_player's passive
        # auto-enrollment-on-first-view above, which stays silent.
        self.client.force_login(self.student)
        self.client.post(reverse('enroll_course', args=[self.course.id]))
        sent = next(m for m in mail.outbox if m.to == ['sub_access_stud@example.com'])
        self.assertIn('Paid Course', sent.subject)


class SubscriptionRevenueDistributionTests(TestCase):
    """Every piastre of a subscriber's payment must land somewhere -- either
    with an instructor or with the platform -- by construction, not luck."""

    def setUp(self):
        self.student = User.objects.create_user(username='rev_stud', password='pw', is_student=True)
        self.instructor_a = User.objects.create_user(
            username='rev_inst_a', password='pw', is_instructor=True)
        self.instructor_b = User.objects.create_user(
            username='rev_inst_b', password='pw', is_instructor=True)
        track = Track.objects.create(name='Rev Track')
        self.course_a = Course.objects.create(
            instructor=self.instructor_a, track=track, title='Course A', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('20.00'),
            status=Course.Status.PUBLISHED)
        self.course_b = Course.objects.create(
            instructor=self.instructor_b, track=track, title='Course B', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, price=Decimal('20.00'),
            status=Course.Status.PUBLISHED)
        module_a = Module.objects.create(course=self.course_a, title='M1')
        module_b = Module.objects.create(course=self.course_b, title='M1')
        self.lecture_a = Lecture.objects.create(module=module_a, title='L1', duration_seconds=3600)
        self.lecture_b = Lecture.objects.create(module=module_b, title='L1', duration_seconds=3600)

        self.plan = Plan.objects.create(
            name='Mendoura Annual Pass', interval=Plan.Interval.ANNUAL,
            price_egp=Decimal('2000.00'), price_usd=Decimal('65.00'))
        self.subscription = Subscription.objects.create(
            student=self.student, plan=self.plan, amount_paid=Decimal('2000.00'),
            expires_at=timezone.now() - timedelta(days=1),  # already ended -> due for distribution
        )
        self.period = SubscriptionPeriod.objects.create(
            subscription=self.subscription,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now() - timedelta(days=1),
            amount_paid=Decimal('2000.00'),
        )

    def _watch(self, lecture, course, seconds, minutes_ago=15):
        WatchEvent.objects.create(
            student=self.student, lecture=lecture, course=course, seconds_watched=seconds,
            occurred_at=self.period.period_start + timedelta(days=1, minutes=minutes_ago))

    def test_worked_example_to_the_piastre(self):
        self._watch(self.lecture_a, self.course_a, 1800)  # 30 min
        self._watch(self.lecture_b, self.course_b, 600)   # 10 min

        call_command('distribute_subscription_revenue')

        dist_a = RevenueDistribution.objects.get(course=self.course_a)
        dist_b = RevenueDistribution.objects.get(course=self.course_b)

        # 1800/2400 = 75% of the EGP 2000 pool
        self.assertEqual(dist_a.attributed_amount, Decimal('1500.00'))
        # Flat 60% subscription split -- NOT course_a's own 70/30 production_type rule
        self.assertEqual(dist_a.instructor_amount, Decimal('900.00'))
        self.assertEqual(dist_a.platform_amount, Decimal('600.00'))

        # 600/2400 = 25%, but course_b is last (ordered by id) so it gets the
        # exact remainder rather than a separately-rounded 25% slice
        self.assertEqual(dist_b.attributed_amount, Decimal('500.00'))
        self.assertEqual(dist_b.instructor_amount, Decimal('300.00'))
        self.assertEqual(dist_b.platform_amount, Decimal('200.00'))

        self.assertEqual(dist_a.attributed_amount + dist_b.attributed_amount, Decimal('2000.00'))

        wallet_a = InstructorWallet.objects.get(instructor=self.instructor_a)
        wallet_b = InstructorWallet.objects.get(instructor=self.instructor_b)
        self.assertEqual(wallet_a.available_balance, Decimal('900.00'))
        self.assertEqual(wallet_b.available_balance, Decimal('300.00'))

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, SubscriptionPeriod.Status.DISTRIBUTED)

    def test_zero_watch_time_does_not_crash_and_keeps_it_all_on_platform(self):
        call_command('distribute_subscription_revenue')
        self.assertFalse(RevenueDistribution.objects.exists())
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, SubscriptionPeriod.Status.DISTRIBUTED)

    def test_job_run_twice_credits_wallets_once(self):
        self._watch(self.lecture_a, self.course_a, 1800)
        self._watch(self.lecture_b, self.course_b, 600)

        call_command('distribute_subscription_revenue')
        call_command('distribute_subscription_revenue')

        wallet_a = InstructorWallet.objects.get(instructor=self.instructor_a)
        self.assertEqual(wallet_a.available_balance, Decimal('900.00'))  # not doubled
        self.assertEqual(RevenueDistribution.objects.filter(course=self.course_a).count(), 1)

    def test_view_under_minimum_threshold_does_not_count(self):
        self._watch(self.lecture_a, self.course_a, 1800)
        self._watch(self.lecture_b, self.course_b, 10)  # under the 30s floor

        call_command('distribute_subscription_revenue')

        self.assertFalse(RevenueDistribution.objects.filter(course=self.course_b).exists())
        dist_a = RevenueDistribution.objects.get(course=self.course_a)
        self.assertEqual(dist_a.attributed_amount, Decimal('2000.00'))  # gets the whole pool

    def test_instructor_watching_own_course_is_excluded(self):
        # instructor_a "watches" their own course -- must not earn from it
        WatchEvent.objects.create(
            student=self.instructor_a, lecture=self.lecture_a, course=self.course_a,
            seconds_watched=1800, occurred_at=self.period.period_start + timedelta(days=1))
        self._watch(self.lecture_b, self.course_b, 600)

        call_command('distribute_subscription_revenue')

        self.assertFalse(RevenueDistribution.objects.filter(course=self.course_a).exists())
        dist_b = RevenueDistribution.objects.get(course=self.course_b)
        self.assertEqual(dist_b.attributed_amount, Decimal('2000.00'))

    def test_rewatching_same_lecture_capped_at_double_duration(self):
        # lecture_a is 3600s long; claim 10x that across several events
        for _ in range(10):
            self._watch(self.lecture_a, self.course_a, 3600)
        self._watch(self.lecture_b, self.course_b, 600)

        call_command('distribute_subscription_revenue')

        dist_a = RevenueDistribution.objects.get(course=self.course_a)
        # Capped at 2x duration (7200s), not the full 36000s claimed
        self.assertEqual(dist_a.seconds_watched, 7200)

    def test_distribution_sums_to_pool_across_random_watch_splits(self):
        courses = []
        for i in range(5):
            instructor = User.objects.create_user(username=f'fuzz_inst_{i}', password='pw', is_instructor=True)
            course = Course.objects.create(
                instructor=instructor, track=self.course_a.track, title=f'Fuzz Course {i}',
                description='...', production_type=Course.ProductionType.FULL,
                price=Decimal('10.00'), status=Course.Status.PUBLISHED)
            module = Module.objects.create(course=course, title='M1')
            lecture = Lecture.objects.create(module=module, title='L1', duration_seconds=36000)
            seconds = random.randint(30, 5000)
            self._watch(lecture, course, seconds)
            courses.append(course)

        call_command('distribute_subscription_revenue')

        distributions = RevenueDistribution.objects.filter(period=self.period)
        total_attributed = sum((d.attributed_amount for d in distributions), Decimal('0.00'))
        self.assertEqual(total_attributed, Decimal('2000.00'))
        for dist in distributions:
            self.assertEqual(dist.instructor_amount + dist.platform_amount, dist.attributed_amount)

    def test_admin_subscription_revenue_page_renders_a_known_distribution(self):
        self._watch(self.lecture_a, self.course_a, 1800)
        self._watch(self.lecture_b, self.course_b, 600)
        call_command('distribute_subscription_revenue')

        admin = User.objects.create_superuser(username='rev_page_admin', password='pw')
        self.client.force_login(admin)
        response = self.client.get(reverse('admin_subscription_revenue'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.course_a.title)
        self.assertContains(response, self.instructor_a.username)
        self.assertContains(response, '900.00')  # instructor_a's share

    def test_direct_sale_split_unaffected_by_subscription_path(self):
        self._watch(self.lecture_a, self.course_a, 1800)
        self._watch(self.lecture_b, self.course_b, 600)
        call_command('distribute_subscription_revenue')

        # A direct one-off sale on course_a (70% production_type=full) must
        # still use its own split rule, not the flat 60% subscription rate.
        payment = Payment.objects.create(
            student=self.student, course=self.course_a, total_amount=Decimal('20.00'))
        self.assertEqual(payment.instructor_amount, Decimal('14.00'))  # 70% of $20
        self.assertEqual(payment.platform_amount, Decimal('6.00'))

    def test_refund_after_distribution_reverses_instructor_credit(self):
        self._watch(self.lecture_a, self.course_a, 1800)
        self._watch(self.lecture_b, self.course_b, 600)
        call_command('distribute_subscription_revenue')

        wallet_a = InstructorWallet.objects.get(instructor=self.instructor_a)
        self.assertEqual(wallet_a.available_balance, Decimal('900.00'))

        self.subscription.provider_transaction_id = 'sub-txn-refund-1'
        self.subscription.save()

        nested, signature = _signed_transaction(
            f'sub{self.plan.id}-student{self.student.id}-abc123',
            id='sub-txn-refund-1', is_refunded=True)

        with override_settings(PAYMOB_HMAC_SECRET=TEST_HMAC_SECRET):
            response = self.client.post(
                f"{reverse('paymob_webhook')}?hmac={signature}",
                data=json.dumps({'obj': nested}), content_type='application/json')
        self.assertEqual(response.status_code, 200)

        wallet_a.refresh_from_db()
        self.assertEqual(wallet_a.available_balance, Decimal('0.00'))
        self.assertTrue(WalletTransaction.objects.filter(
            wallet=wallet_a, type=WalletTransaction.Type.REFUND_DEBIT, amount=Decimal('900.00')).exists())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Subscription.Status.CANCELED)


class SignupDuplicateGuardTests(TestCase):
    def test_duplicate_username_shows_friendly_error(self):
        User.objects.create_user(username='taken', password='pw', email='a@example.com')
        response = self.client.post(reverse('student_signup'), {
            'username': 'taken', 'email': 'b@example.com',
            'password1': 'a-strong-password-1', 'password2': 'a-strong-password-1',
        })
        self.assertContains(response, 'An account with this username already exists.')
        self.assertEqual(User.objects.filter(username='taken').count(), 1)

    def test_duplicate_email_shows_friendly_error(self):
        User.objects.create_user(username='first', password='pw', email='dup@example.com')
        response = self.client.post(reverse('student_signup'), {
            'username': 'second', 'email': 'dup@example.com',
            'password1': 'a-strong-password-1', 'password2': 'a-strong-password-1',
        })
        self.assertContains(response, 'An account with this email already exists.')
        self.assertFalse(User.objects.filter(username='second').exists())

    def test_duplicate_phone_number_shows_friendly_error(self):
        User.objects.create_user(username='first_phone', password='pw', phone_number='+201001234567')
        response = self.client.post(reverse('student_signup'), {
            'username': 'second_phone', 'email': 'c@example.com', 'phone_number': '+201001234567',
            'password1': 'a-strong-password-1', 'password2': 'a-strong-password-1',
        })
        self.assertContains(response, 'An account with this phone number already exists.')
        self.assertFalse(User.objects.filter(username='second_phone').exists())


class CheckoutFlowTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='checkout_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='checkout_stud', password='pw', is_student=True)
        track = Track.objects.create(name='Cybersecurity')
        self.paid_course = Course.objects.create(
            instructor=self.instructor, track=track, title='Paid Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('30.00'),
            status=Course.Status.PUBLISHED,
        )

    def test_enroll_on_paid_course_redirects_to_checkout(self):
        self.client.force_login(self.student)
        response = self.client.post(reverse('enroll_course', args=[self.paid_course.id]))
        self.assertRedirects(response, reverse('checkout_course', args=[self.paid_course.id]),
                              fetch_redirect_response=False)
        self.assertFalse(Enrollment.objects.filter(student=self.student).exists())

    def test_checkout_page_shows_both_purchase_options(self):
        Plan.objects.create(name='Mendoura Annual Pass', price_egp=Decimal('1499.00'), price_usd=Decimal('49.00'))
        self.client.force_login(self.student)
        response = self.client.get(reverse('checkout_course', args=[self.paid_course.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Buy This Course')
        self.assertContains(response, 'Get the Annual Pass')

    @patch('courses.views.paymob.initiate_checkout')
    def test_checkout_course_option_redirects_to_paymob_iframe(self, mock_initiate):
        mock_initiate.return_value = 'https://accept.paymob.com/api/acceptance/iframes/1?payment_token=abc'
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('checkout_course', args=[self.paid_course.id]), {'option': 'course'})
        self.assertRedirects(
            response, 'https://accept.paymob.com/api/acceptance/iframes/1?payment_token=abc',
            fetch_redirect_response=False)
        mock_initiate.assert_called_once()
        amount_cents = mock_initiate.call_args[0][0]
        self.assertEqual(amount_cents, 3000)  # course price, not the subscription price

    @patch('courses.views.paymob.initiate_checkout')
    def test_checkout_subscription_option_redirects_to_paymob_iframe(self, mock_initiate):
        plan = Plan.objects.create(name='Mendoura Annual Pass', price_egp=Decimal('1499.00'), price_usd=Decimal('49.00'))
        mock_initiate.return_value = 'https://accept.paymob.com/api/acceptance/iframes/2?payment_token=xyz'
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('checkout_course', args=[self.paid_course.id]),
            {'option': 'subscription', 'plan_id': plan.id})
        self.assertRedirects(
            response, 'https://accept.paymob.com/api/acceptance/iframes/2?payment_token=xyz',
            fetch_redirect_response=False)
        amount_cents = mock_initiate.call_args[0][0]
        self.assertEqual(amount_cents, 149900)  # plan price in EGP cents, not the course price

    def test_already_enrolled_student_cannot_start_checkout_again(self):
        Enrollment.objects.create(student=self.student, course=self.paid_course)
        self.client.force_login(self.student)
        response = self.client.get(reverse('checkout_course', args=[self.paid_course.id]))
        self.assertRedirects(response, reverse('course_detail', args=[self.paid_course.id]))


class SeedAdminCommandTests(TestCase):
    """The only way to get an admin login on a Shell-less Render free plan
    is this command running at build time, so it must actually work."""

    def test_noop_without_env_vars(self):
        call_command('seed_admin')
        self.assertFalse(User.objects.filter(is_superuser=True).exists())

    def test_creates_superuser_when_env_vars_set(self):
        with patch('courses.management.commands.seed_admin.config') as mock_config:
            mock_config.side_effect = lambda key, default='': {
                'DJANGO_SUPERUSER_USERNAME': 'siteadmin',
                'DJANGO_SUPERUSER_PASSWORD': 'a-strong-password-1',
                'DJANGO_SUPERUSER_EMAIL': 'admin@example.com',
            }.get(key, default)
            call_command('seed_admin')

        admin = User.objects.get(username='siteadmin')
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.check_password('a-strong-password-1'))

    def test_idempotent_on_second_run(self):
        with patch('courses.management.commands.seed_admin.config') as mock_config:
            mock_config.side_effect = lambda key, default='': {
                'DJANGO_SUPERUSER_USERNAME': 'siteadmin2',
                'DJANGO_SUPERUSER_PASSWORD': 'a-strong-password-1',
            }.get(key, default)
            call_command('seed_admin')
            call_command('seed_admin')

        self.assertEqual(User.objects.filter(username='siteadmin2').count(), 1)


class HealthCheckTests(TestCase):
    def test_healthz_returns_200_without_authentication(self):
        response = self.client.get('/healthz/')
        self.assertEqual(response.status_code, 200)


class LoginRedirectTests(TestCase):
    """LOGIN_URL must point at this project's actual /login/ route --
    Django's unconfigured default (/accounts/login/) has no URL pattern
    here, so every @login_required view (including the whole Admin Panel)
    would 404 instead of prompting a signed-out visitor to log in."""

    def test_login_required_view_redirects_to_real_login_page_not_404(self):
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))
        follow = self.client.get(response.url)
        self.assertEqual(follow.status_code, 200)

    def test_send_test_emails_redirects_to_login_not_404(self):
        response = self.client.get(reverse('send_test_emails'), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Login')


class RoleAwarePostLoginRedirectTests(TestCase):
    """After the welcome email promises "you now have access to your
    instructor dashboard", logging in must actually land an approved
    Instructor there -- not on the generic marketing homepage aimed at
    signed-out visitors ("Start Learning" / "Become an Instructor")."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='login_redirect_inst', password='pw12345678',
            is_instructor=True, is_approved=True, email='inst@example.com')
        self.student = User.objects.create_user(
            username='login_redirect_stud', password='pw12345678',
            is_student=True, is_approved=True, email='stud@example.com')

    def test_approved_instructor_login_redirects_to_instructor_dashboard(self):
        response = self.client.post(
            reverse('login'), {'username': 'login_redirect_inst', 'password': 'pw12345678'})
        self.assertRedirects(response, reverse('instructor_dashboard'))

    def test_instructor_dashboard_is_reachable_after_login(self):
        self.client.login(username='login_redirect_inst', password='pw12345678')
        response = self.client.get(reverse('instructor_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'login_redirect_inst')

    def test_explicit_next_still_wins_over_role_redirect(self):
        response = self.client.post(
            f"{reverse('login')}?next={reverse('create_course')}",
            {'username': 'login_redirect_inst', 'password': 'pw12345678'})
        self.assertRedirects(response, reverse('create_course'))

    def test_student_login_still_goes_to_homepage(self):
        response = self.client.post(
            reverse('login'), {'username': 'login_redirect_stud', 'password': 'pw12345678'})
        self.assertRedirects(response, reverse('platform_home'))

    def test_homepage_shows_dashboard_link_for_logged_in_instructor(self):
        self.client.login(username='login_redirect_inst', password='pw12345678')
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'Go to Your Dashboard')
        self.assertNotContains(response, 'Become an Instructor')

    def test_homepage_shows_continue_learning_link_for_logged_in_student(self):
        # Regression test: the instructor-only branch added to the homepage
        # hero CTA left students falling through to the same "Start
        # Learning" / "Become an Instructor" pitch shown to a logged-out
        # visitor -- nonsensical for someone already learning.
        self.client.login(username='login_redirect_stud', password='pw12345678')
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'Continue Learning')
        self.assertNotContains(response, 'Start Learning')
        self.assertNotContains(response, 'Become an Instructor')

    def test_homepage_shows_marketing_ctas_for_logged_out_visitor(self):
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, 'Start Learning')
        self.assertContains(response, 'Become an Instructor')


class EmailTimeoutConfigTests(TestCase):
    """Without EMAIL_TIMEOUT, smtplib's socket has no timeout at all -- a
    slow/unreachable SMTP host hangs the request indefinitely, which no
    try/except in emails.py can catch (confirmed locally: a real send
    attempt against an unreachable host blocked for 40+ seconds with
    EMAIL_TIMEOUT unset, and returned in ~10s once it was set). That's what
    an un-graceful, non-Django 500 (the WSGI worker's own timeout killing
    the request) looks like from the browser -- this just asserts the
    safety net stays configured."""

    def test_email_timeout_is_configured(self):
        self.assertIsNotNone(settings.EMAIL_TIMEOUT)
        self.assertGreater(settings.EMAIL_TIMEOUT, 0)


class AdminSiteApprovalVisibilityTests(TestCase):
    """is_approved defaults to True at the model level (intentional, so
    trusted creation paths like the auto-seeded superuser aren't locked
    out) -- but Django's stock UserAdmin never exposed the field, so
    creating/editing an Instructor via /admin/ silently inherited that
    True default with no visibility into it. That's a real, reproduced
    path to an "approved" instructor account that never went through
    approve_user()."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username='site_admin', password='pw')
        self.client.force_login(self.admin)

    def test_is_approved_visible_on_user_change_form(self):
        instructor = User.objects.create_user(
            username='visibility_inst', password='pw', is_instructor=True, is_approved=True)
        response = self.client.get(f'/admin/courses/user/{instructor.id}/change/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="is_approved"')

    def test_is_approved_visible_in_user_list(self):
        response = self.client.get('/admin/courses/user/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'column-is_approved')


class PWATests(TestCase):
    def test_manifest_is_valid_json_with_correct_content_type(self):
        response = self.client.get('/manifest.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json')
        data = json.loads(response.content)
        self.assertEqual(data['name'], 'Mendoura LMS')
        self.assertEqual(data['short_name'], 'Mendoura')
        self.assertEqual(data['start_url'], '/')
        self.assertEqual(data['display'], 'standalone')
        self.assertEqual(data['background_color'], '#030712')
        self.assertEqual(data['theme_color'], '#030712')
        sizes = {icon['sizes'] for icon in data['icons']}
        self.assertEqual(sizes, {'192x192', '512x512'})
        for icon in data['icons']:
            self.assertTrue(icon['src'].startswith('/static/img/android-'))

    def test_service_worker_served_at_root_with_correct_content_type(self):
        response = self.client.get('/service-worker.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/javascript')
        content = response.content.decode()
        self.assertIn("addEventListener('fetch'", content)
        # Must never intercept/cache third-party media -- the whole point
        # of the bypass list is that these hosts are never touched.
        self.assertIn('res.cloudinary.com', content)
        self.assertIn('video.bunnycdn.com', content)

    @override_settings(STATIC_ASSET_VERSION='deadbeef123')
    def test_service_worker_cache_name_tracks_static_asset_version(self):
        # Regression coverage: CACHE_NAME used to be a hardcoded literal that
        # never changed across deploys, so the service worker's own
        # stale-while-revalidate caching for /static/ (including
        # tailwind.css, which STORAGES deliberately serves unversioned --
        # see settings.py) could keep serving a pre-deploy CSS/asset
        # indefinitely, since 'activate' only evicts caches whose name
        # differs from the current CACHE_NAME.
        response = self.client.get('/service-worker.js')
        self.assertIn('deadbeef123', response.content.decode())

    @override_settings(STATIC_ASSET_VERSION='deadbeef123')
    def test_tailwind_css_link_is_cache_busted_with_static_asset_version(self):
        response = self.client.get('/')
        self.assertContains(response, 'tailwind.css?v=deadbeef123')

    def test_offline_page_renders(self):
        response = self.client.get('/offline/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You're offline")

    def test_manifest_link_present_on_every_page(self):
        response = self.client.get('/')
        self.assertContains(response, '/manifest.json')
        self.assertContains(response, 'serviceWorker')

    def test_assetlinks_served_at_wellknown_path_with_correct_content_type(self):
        response = self.client.get('/.well-known/assetlinks.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        entry = data[0]
        self.assertEqual(entry['relation'], ['delegate_permission/common.handle_all_urls'])
        self.assertEqual(entry['target']['namespace'], 'android_app')
        self.assertEqual(entry['target']['package_name'], 'com.mendoura.twa')
        self.assertEqual(entry['target']['sha256_cert_fingerprints'], [
            '9B:68:56:66:B6:4B:E9:88:71:AE:52:89:C8:B3:28:BF:FA:42:9F:95:3E:CA:B9:70:36:BE:29:8D:79:D9:7A:75',
        ])


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='test-api-key', BUNNY_TOKEN_KEY='')
class BunnyHelperTests(TestCase):
    def test_upload_credentials_signature_matches_bunny_scheme(self):
        from courses import bunny
        creds = bunny.upload_credentials('vid-123')
        expected = hashlib.sha256(
            f"705216test-api-key{creds['expiration']}vid-123".encode()).hexdigest()
        self.assertEqual(creds['signature'], expected)
        self.assertEqual(creds['video_id'], 'vid-123')
        self.assertEqual(creds['library_id'], '705216')
        # The raw API key must never be handed to the browser.
        self.assertNotIn('test-api-key', str(creds))

    def test_embed_url_is_plain_without_token_key(self):
        from courses import bunny
        self.assertEqual(
            bunny.embed_url('vid-123'),
            'https://iframe.mediadelivery.net/embed/705216/vid-123')

    @override_settings(BUNNY_TOKEN_KEY='secret-token-key')
    def test_embed_url_is_signed_when_token_key_present(self):
        from courses import bunny
        url = bunny.embed_url('vid-123')
        self.assertIn('token=', url)
        self.assertIn('expires=', url)
        self.assertNotIn('secret-token-key', url)  # the key is hashed, never exposed


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='test-api-key')
class BunnyUploadEndpointTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='bunny_inst', password='pw', is_instructor=True)
        self.intruder = User.objects.create_user(
            username='bunny_intruder', password='pw', is_instructor=True)
        parent = Track.objects.create(name='Bunny Parent')
        track = Track.objects.create(name='Bunny Track', parent=parent)
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Bunny Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        self.module = Module.objects.create(course=self.course, title='M1')
        self.lecture = Lecture.objects.create(module=self.module, title='L1')

    @patch('courses.bunny.create_video', return_value='new-guid-123')
    def test_create_video_stores_guid_and_returns_credentials(self, mock_create):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('create_bunny_video', args=[self.lecture.id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['video_id'], 'new-guid-123')
        self.assertIn('signature', data)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_video_id, 'new-guid-123')

    @patch('courses.bunny.create_video', return_value='new-guid-123')
    def test_create_video_on_published_course_reenters_review(self, mock_create):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_bunny_video', args=[self.lecture.id]))
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

    @patch('courses.bunny.create_video')
    def test_non_owner_cannot_create_video_for_anothers_lecture(self, mock_create):
        self.client.force_login(self.intruder)
        response = self.client.post(reverse('create_bunny_video', args=[self.lecture.id]))
        self.assertEqual(response.status_code, 404)
        mock_create.assert_not_called()  # the Bunny API is never even reached

    @override_settings(BUNNY_LIBRARY_ID='', BUNNY_API_KEY='')
    def test_returns_503_when_bunny_not_configured(self):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('create_bunny_video', args=[self.lecture.id]))
        self.assertEqual(response.status_code, 503)

    @patch('courses.bunny.create_video', side_effect=requests.ConnectionError('connection refused'))
    def test_upload_start_failure_returns_502_and_logs_real_reason(self, mock_create):
        # Regression test for the "Could not start the upload" report with
        # no trace of why anywhere -- create_bunny_video's except block
        # used to swallow BunnyError/RequestException with no logging at
        # all. The real exception must now be logged (tagged
        # [BUNNY_UPLOAD_DEBUG]) even though the frontend still only sees
        # the generic message.
        self.client.force_login(self.instructor)
        with self.assertLogs('courses.views', level='ERROR') as logs:
            response = self.client.post(reverse('create_bunny_video', args=[self.lecture.id]))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'error': 'Could not start the upload. Please try again.'})
        self.assertTrue(any('[BUNNY_UPLOAD_DEBUG]' in message for message in logs.output))
        self.assertTrue(any(str(self.lecture.id) in message for message in logs.output))

    @override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='test-api-key')
    def test_edit_lecture_page_loads_upload_library_locally_not_from_a_cdn(self):
        # Regression test: the upload button used to depend on tus-js-client
        # loading from cdn.jsdelivr.net at runtime -- any CDN hiccup (or a
        # network that blocks it, as we've seen happen with other CDNs this
        # project used) left the "Upload Video" button silently stuck
        # disabled with no way to recover short of a page reload. Vendoring
        # the script removes that single point of failure entirely.
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('edit_lecture', args=[self.lecture.id]))
        self.assertContains(response, '/static/js/tus.min.js')
        self.assertNotContains(response, 'cdn.jsdelivr.net')


class BunnyWebhookTests(TestCase):
    def setUp(self):
        inst = User.objects.create_user(username='bw_inst', password='pw', is_instructor=True)
        track = Track.objects.create(name='BW Track')
        course = Course.objects.create(
            instructor=inst, track=track, title='BW Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True)
        module = Module.objects.create(course=course, title='M1')
        self.lecture = Lecture.objects.create(
            module=module, title='L1', bunny_video_id='guid-xyz', bunny_status=0)

    def test_webhook_updates_status_by_guid(self):
        response = self.client.post(
            reverse('bunny_webhook'),
            data=json.dumps({'VideoGuid': 'guid-xyz', 'Status': 4}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_status, 4)
        self.assertTrue(self.lecture.bunny_ready)

    def test_webhook_ignores_unknown_guid(self):
        response = self.client.post(
            reverse('bunny_webhook'),
            data=json.dumps({'VideoGuid': 'nonexistent', 'Status': 4}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_status, 0)


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='test-api-key')
class BunnyStatusHelperTests(TestCase):
    """get_video_status is the polling fallback for when Bunny's webhook
    delivery is missed (e.g. a Render free-tier dyno asleep at delivery
    time) -- it must surface Bunny's real status or the real failure, the
    same way create_video already does."""

    @patch('courses.bunny.requests.get')
    def test_get_video_status_returns_int_status(self, mock_get):
        from courses import bunny
        mock_get.return_value = Mock(ok=True, status_code=200, text='{"status": 4}',
                                      json=lambda: {'status': 4})
        self.assertEqual(bunny.get_video_status('guid-1'), 4)

    @patch('courses.bunny.requests.get')
    def test_get_video_info_returns_status_and_length(self, mock_get):
        from courses import bunny
        mock_get.return_value = Mock(
            ok=True, status_code=200, text='{"status": 4, "length": 245}',
            json=lambda: {'status': 4, 'length': 245})
        self.assertEqual(bunny.get_video_info('guid-1'), {'status': 4, 'length': 245})

    @patch('courses.bunny.requests.get')
    def test_get_video_info_defaults_length_to_zero_when_absent(self, mock_get):
        from courses import bunny
        mock_get.return_value = Mock(ok=True, status_code=200, text='{"status": 1}',
                                      json=lambda: {'status': 1})
        self.assertEqual(bunny.get_video_info('guid-1'), {'status': 1, 'length': 0})

    @patch('courses.bunny.requests.get')
    def test_get_video_status_raises_on_error_response(self, mock_get):
        from courses import bunny
        response = Mock(ok=False, status_code=401, text='{"Message": "Invalid AccessKey"}')
        response.raise_for_status.side_effect = requests.HTTPError('401 Unauthorized')
        mock_get.return_value = response
        with self.assertRaises(requests.HTTPError):
            bunny.get_video_status('guid-1')

    @patch('courses.bunny.requests.get', side_effect=requests.ConnectionError('refused'))
    def test_get_video_status_logs_request_failure(self, mock_get):
        from courses import bunny
        with self.assertLogs('courses.bunny', level='ERROR') as logs:
            with self.assertRaises(requests.ConnectionError):
                bunny.get_video_status('guid-1')
        self.assertTrue(any('[BUNNY_STATUS_DEBUG]' in message for message in logs.output))


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='test-api-key')
class BunnyStatusSyncOnPageLoadTests(TestCase):
    """Regression coverage for lectures stuck showing "still processing"
    long after Bunny actually finished: bunny_status used to be updated
    ONLY by Bunny's webhook, with no fallback if delivery was missed. Both
    the wizard's Content step and the classic edit_lecture page must now
    poll Bunny's real status on load and update the stale local flag."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='bss_inst', password='pw', is_instructor=True)
        parent = Track.objects.create(name='BSS Parent')
        track = Track.objects.create(name='BSS Track', parent=parent)
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='BSS Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.DRAFT)
        self.module = Module.objects.create(course=self.course, title='M1')
        self.lecture = Lecture.objects.create(
            module=self.module, title='L1', bunny_video_id='guid-stuck', bunny_status=1)
        self.client.force_login(self.instructor)

    @patch('courses.bunny.get_video_info', return_value={'status': 4, 'length': 245})
    def test_wizard_content_step_refreshes_stale_status(self, mock_info):
        response = self.client.get(
            reverse('course_wizard_module_content', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 200)
        mock_info.assert_called_once_with('guid-stuck')
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_status, 4)
        self.assertEqual(self.lecture.duration_seconds, 245)
        self.assertContains(response, 'Video ready')
        self.assertNotContains(response, 'still processing')

    @patch('courses.bunny.get_video_info', return_value={'status': 4, 'length': 245})
    def test_edit_lecture_page_refreshes_stale_status(self, mock_info):
        response = self.client.get(reverse('edit_lecture', args=[self.lecture.id]))
        self.assertEqual(response.status_code, 200)
        mock_info.assert_called_once_with('guid-stuck')
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_status, 4)
        self.assertEqual(self.lecture.duration_seconds, 245)

    @patch('courses.bunny.get_video_info')
    def test_already_ready_lecture_does_not_call_bunny_again(self, mock_info):
        self.lecture.bunny_status = 4
        self.lecture.duration_seconds = 245
        self.lecture.save(update_fields=['bunny_status', 'duration_seconds'])
        response = self.client.get(
            reverse('course_wizard_module_content', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 200)
        mock_info.assert_not_called()

    @patch('courses.bunny.get_video_info')
    def test_ready_lecture_missing_duration_still_backfills_it(self, mock_info):
        mock_info.return_value = {'status': 4, 'length': 245}
        self.lecture.bunny_status = 4
        self.lecture.save(update_fields=['bunny_status'])
        response = self.client.get(
            reverse('course_wizard_module_content', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 200)
        mock_info.assert_called_once_with('guid-stuck')
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.duration_seconds, 245)

    @patch('courses.bunny.get_video_info', side_effect=requests.ConnectionError('refused'))
    def test_bunny_failure_during_sync_does_not_break_the_page(self, mock_info):
        with self.assertLogs('courses.views', level='ERROR') as logs:
            response = self.client.get(
                reverse('course_wizard_module_content', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 200)
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.bunny_status, 1)  # unchanged -- still "processing"
        self.assertEqual(self.lecture.duration_seconds, 0)
        self.assertTrue(any('[BUNNY_STATUS_DEBUG]' in message for message in logs.output))


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='k', BUNNY_TOKEN_KEY='tok')
class BunnyPlayerEmbedTests(TestCase):
    @patch('courses.bunny.get_video_info', return_value={'status': 4, 'length': 200})
    def test_player_embeds_signed_bunny_iframe(self, mock_info):
        inst = User.objects.create_user(username='bp_inst', password='pw', is_instructor=True)
        student = User.objects.create_user(username='bp_stud', password='pw', is_student=True)
        track = Track.objects.create(name='BP Track')
        course = Course.objects.create(
            instructor=inst, track=track, title='BP Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=course, title='M1')
        lecture = Lecture.objects.create(
            module=module, title='L1', bunny_video_id='guid-abc', bunny_status=4, is_preview=True)
        Enrollment.objects.create(student=student, course=course)

        self.client.force_login(student)
        response = self.client.get(reverse('course_player', args=[course.id, lecture.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'iframe.mediadelivery.net/embed/705216/guid-abc')
        self.assertContains(response, 'token=')


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='k')
class CoursePlayerBackfillsEveryLectureTests(TestCase):
    """The Overview tab's total-duration sum needs every lecture in the
    course backfilled, not just the one being watched -- course_player now
    syncs every lecture on each load (each is a fast no-op once it already
    has a ready status and a known duration, so this doesn't keep costing
    Bunny API calls forever once a course is fully backfilled)."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='cpb_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(username='cpb_stud', password='pw', is_student=True)
        track = Track.objects.create(name='CPB Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='CPB Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=self.course, title='M1')
        self.lecture1 = Lecture.objects.create(
            module=module, title='L1', bunny_video_id='guid-1', bunny_status=4, order=1)
        self.lecture2 = Lecture.objects.create(
            module=module, title='L2', bunny_video_id='guid-2', bunny_status=4, order=2)
        Enrollment.objects.create(student=self.student, course=self.course)
        self.client.force_login(self.student)

    @patch('courses.bunny.get_video_info')
    def test_viewing_one_lecture_backfills_duration_for_every_lecture_in_the_course(self, mock_info):
        mock_info.side_effect = lambda video_id: {
            'guid-1': {'status': 4, 'length': 100},
            'guid-2': {'status': 4, 'length': 200},
        }[video_id]

        self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))

        self.lecture1.refresh_from_db()
        self.lecture2.refresh_from_db()
        self.assertEqual(self.lecture1.duration_seconds, 100)
        self.assertEqual(self.lecture2.duration_seconds, 200)
        self.assertEqual(mock_info.call_count, 2)

    @patch('courses.bunny.get_video_info')
    def test_already_backfilled_course_makes_no_further_bunny_calls(self, mock_info):
        self.lecture1.duration_seconds = 100
        self.lecture1.save(update_fields=['duration_seconds'])
        self.lecture2.duration_seconds = 200
        self.lecture2.save(update_fields=['duration_seconds'])

        self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))

        mock_info.assert_not_called()

    @patch('courses.bunny.get_video_info')
    def test_retries_a_lecture_that_previously_got_a_zero_length_back(self, mock_info):
        # lecture2's earlier sync got status=ready but length=0 (video not
        # fully processed by Bunny yet at the time) -- still 0 in the DB.
        # A later page view, once Bunny actually has the length, must not
        # have given up after that first null response.
        mock_info.side_effect = lambda video_id: {
            'guid-1': {'status': 4, 'length': 100},
            'guid-2': {'status': 4, 'length': 200},  # now available
        }[video_id]

        self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))

        self.lecture2.refresh_from_db()
        self.assertEqual(self.lecture2.duration_seconds, 200)


class DurationDisplayFilterTests(TestCase):
    """The `duration_display` template filter (courses/templatetags/
    course_extras.py) used by the lesson player's Contents sidebar."""

    def test_minutes_and_seconds(self):
        from courses.templatetags.course_extras import duration_display
        self.assertEqual(duration_display(246), '4m 6s')

    def test_whole_minutes_only(self):
        from courses.templatetags.course_extras import duration_display
        self.assertEqual(duration_display(120), '2m')

    def test_seconds_only(self):
        from courses.templatetags.course_extras import duration_display
        self.assertEqual(duration_display(45), '45s')

    def test_zero_or_falsy_returns_empty_string(self):
        # duration_seconds isn't populated by any upload path yet, so most
        # real lectures are 0 -- showing nothing beats a misleading "0m 0s".
        from courses.templatetags.course_extras import duration_display
        self.assertEqual(duration_display(0), '')
        self.assertEqual(duration_display(None), '')


class LessonPlayerLayoutTests(TestCase):
    """The redesigned lesson-viewing page (Contents sidebar + video +
    Overview tab), structured after LinkedIn Learning's course player.
    course_player's access-control logic and the Bunny embed itself are
    unchanged -- these tests cover only the new layout."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='layout_inst', password='pw', is_instructor=True,
            first_name='Jane', last_name='Teacher')
        self.student = User.objects.create_user(
            username='layout_stud', password='pw', is_student=True)
        track = Track.objects.create(name='Layout Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Layout Course',
            description='A course about layouts.',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        self.module1 = Module.objects.create(course=self.course, title='Intro', order=1)
        self.lecture1 = Lecture.objects.create(
            module=self.module1, title='Welcome', duration_seconds=246, order=1)
        self.module2 = Module.objects.create(course=self.course, title='Deep Dive', order=2)
        self.lecture2 = Lecture.objects.create(
            module=self.module2, title='Details', duration_seconds=0, order=1)
        self.article_lecture = Lecture.objects.create(
            module=self.module2, title='Reading', content_type=Lecture.ContentType.ARTICLE, order=2)
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def test_progress_summary_shown_for_enrolled_student(self):
        LectureProgress.objects.create(enrollment=self.enrollment, lecture=self.lecture1, completed=True)
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture2.id]))
        self.assertContains(response, '33% ')  # 1 of 3 lectures complete, rounded
        self.assertContains(response, '1 of 3 lessons')

    def test_progress_summary_hidden_without_an_enrollment(self):
        # An admin/owner previewing the course has no Enrollment, so there's
        # no per-student progress to show.
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture2.id]))
        self.assertNotContains(response, 'lessons')

    def test_current_lecture_module_is_expanded_others_collapsed(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture2.id]))
        content = response.content.decode()
        # module2 (containing the current lecture) renders visible -- search
        # for the div's own id="..." (not the toggle button's data-target=
        # "...", which names the same string but appears earlier in the DOM).
        module2_start = content.index(f'id="module-content-{self.module2.id}"')
        module2_div = content[module2_start:module2_start + 200]
        self.assertNotIn('hidden', module2_div.split('>')[0])
        # ...module1 renders with the hidden class.
        module1_start = content.index(f'id="module-content-{self.module1.id}"')
        module1_div = content[module1_start:module1_start + 200]
        self.assertIn('hidden', module1_div.split('>')[0])

    def test_completed_lecture_shows_checkmark_not_started_shows_empty_circle(self):
        LectureProgress.objects.create(enrollment=self.enrollment, lecture=self.lecture1, completed=True)
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, 'fa-circle-check')
        self.assertContains(response, 'fa-regular fa-circle')

    def test_video_lecture_with_duration_shows_humanized_duration(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, '4m 6s video')

    def test_video_lecture_without_duration_just_says_video(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture2.id]))
        self.assertContains(response, 'Video')

    def test_article_lecture_shows_article_label(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, 'Article')

    def test_module_quiz_shown_as_quiz_type(self):
        quiz = Quiz.objects.create(module=self.module2, passing_score_percent=70)
        question = Question.objects.create(quiz=quiz, text='2+2=?', order=1)
        Choice.objects.create(question=question, text='4', is_correct=True)
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, reverse('take_quiz', args=[self.course.id, self.module2.id]))
        self.assertContains(response, 'Quiz')

    def test_overview_tab_shows_course_description_and_instructor(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, 'A course about layouts.')
        self.assertContains(response, 'Jane Teacher')

    def test_sidebar_collapse_and_expand_controls_present(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, 'id="sidebar-collapse-btn"')
        self.assertContains(response, 'id="sidebar-expand-btn"')


class WatchThresholdAutoCompleteTests(TestCase):
    """record_watch_event's watch-threshold-gated auto-complete: once a
    student's total logged watch-time on a lecture reaches
    WATCH_COMPLETE_THRESHOLD (90%) of its known duration, it's marked
    complete automatically -- no manual "Mark as Complete" click needed."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='wtac_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(username='wtac_stud', password='pw', is_student=True)
        track = Track.objects.create(name='WTAC Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='WTAC Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=self.course, title='M1')
        self.lecture = Lecture.objects.create(module=module, title='L1', duration_seconds=100)
        # A second, never-completed lecture keeps the enrollment permanently
        # incomplete -- issue_certificate_if_complete() short-circuits before
        # ever calling generate_pdf(), so these tests don't need real
        # Cloudinary credentials just to complete lecture 1.
        Lecture.objects.create(module=module, title='L2')
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)
        self.client.force_login(self.student)

    def _watch(self, seconds):
        return self.client.post(
            reverse('record_watch_event', args=[self.course.id, self.lecture.id]),
            data=json.dumps({'seconds': seconds}), content_type='application/json')

    def test_reaching_90_percent_auto_completes(self):
        # A prior event 10 minutes ago clears the anti-spoofing elapsed-time
        # checks (which only apply relative to the student's *last* event).
        WatchEvent.objects.create(
            student=self.student, lecture=self.lecture, course=self.course,
            seconds_watched=50, occurred_at=timezone.now() - timedelta(minutes=10))
        response = self._watch(45)  # 50 + 45 = 95 -- crosses the 90/100 threshold
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['completed'])
        progress = LectureProgress.objects.get(enrollment=self.enrollment, lecture=self.lecture)
        self.assertTrue(progress.completed)

    def test_under_threshold_does_not_complete(self):
        response = self._watch(30)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['completed'])
        self.assertFalse(
            LectureProgress.objects.filter(
                enrollment=self.enrollment, lecture=self.lecture, completed=True).exists())

    def test_unknown_duration_never_auto_completes(self):
        self.lecture.duration_seconds = 0
        self.lecture.save(update_fields=['duration_seconds'])
        response = self._watch(90)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['completed'])

    def test_already_completed_lecture_reports_completed_without_reprocessing(self):
        LectureProgress.objects.create(enrollment=self.enrollment, lecture=self.lecture, completed=True)
        response = self._watch(10)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['completed'])

    def test_manual_mark_complete_still_works_independently(self):
        response = self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.assertEqual(response.status_code, 302)
        progress = LectureProgress.objects.get(enrollment=self.enrollment, lecture=self.lecture)
        self.assertTrue(progress.completed)


class ContinueLearningTests(TestCase):
    """'Continue Learning' (homepage) and 'View Course' (My Learning) used to
    both funnel an already-enrolled student through the marketing-style
    course detail page before they could reach the player. Both now jump
    straight into course_player at the next incomplete lecture."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='cl_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(username='cl_stud', password='pw', is_student=True)
        track = Track.objects.create(name='CL Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='CL Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=self.course, title='M1')
        self.lecture1 = Lecture.objects.create(module=module, title='L1', order=1)
        self.lecture2 = Lecture.objects.create(module=module, title='L2', order=2)
        self.lecture3 = Lecture.objects.create(module=module, title='L3', order=3)
        self.client.force_login(self.student)

    def test_anonymous_user_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse('continue_learning'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_non_student_redirected_to_home(self):
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('platform_home'))

    def test_no_enrollments_redirects_to_browse_tracks(self):
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('track_list'))

    def test_never_started_course_jumps_to_first_lecture(self):
        Enrollment.objects.create(student=self.student, course=self.course)
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('course_player', args=[self.course.id, self.lecture1.id]))

    def test_in_progress_course_jumps_to_next_incomplete_lecture(self):
        enrollment = Enrollment.objects.create(student=self.student, course=self.course)
        LectureProgress.objects.create(enrollment=enrollment, lecture=self.lecture1, completed=True)
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('course_player', args=[self.course.id, self.lecture2.id]))

    def test_fully_completed_course_reopens_from_the_start(self):
        enrollment = Enrollment.objects.create(student=self.student, course=self.course)
        for lecture in (self.lecture1, self.lecture2, self.lecture3):
            LectureProgress.objects.create(enrollment=enrollment, lecture=lecture, completed=True)
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('course_player', args=[self.course.id, self.lecture1.id]))

    def test_picks_the_most_recently_watched_course_over_most_recently_enrolled(self):
        track = self.course.track
        older_course = self.course
        newer_course = Course.objects.create(
            instructor=self.instructor, track=track, title='Newer Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        newer_module = Module.objects.create(course=newer_course, title='M1')
        Lecture.objects.create(module=newer_module, title='NL1', order=1)

        Enrollment.objects.create(student=self.student, course=older_course)
        # Enrolled in newer_course more recently, but actually watching older_course.
        Enrollment.objects.create(student=self.student, course=newer_course)
        WatchEvent.objects.create(
            student=self.student, course=older_course, lecture=self.lecture1, seconds_watched=30)

        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('course_player', args=[older_course.id, self.lecture1.id]))

    def test_course_with_no_lectures_falls_back_to_course_detail(self):
        empty_course = Course.objects.create(
            instructor=self.instructor, track=self.course.track, title='Empty Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        Enrollment.objects.create(student=self.student, course=empty_course)
        response = self.client.get(reverse('continue_learning'))
        self.assertRedirects(response, reverse('course_detail', args=[empty_course.id]))

    def test_my_learning_view_course_link_targets_player_for_in_progress_enrollment(self):
        Enrollment.objects.create(student=self.student, course=self.course)
        response = self.client.get(reverse('my_learning'))
        self.assertContains(response, reverse('course_player', args=[self.course.id, self.lecture1.id]))
        self.assertContains(response, 'Continue Learning')

    def test_my_learning_view_course_link_falls_back_to_detail_for_completed_enrollment(self):
        enrollment = Enrollment.objects.create(student=self.student, course=self.course)
        for lecture in (self.lecture1, self.lecture2, self.lecture3):
            LectureProgress.objects.create(enrollment=enrollment, lecture=lecture, completed=True)
        # Bypasses issue_certificate_if_complete()'s real PDF generation
        # (Cloudinary isn't configured in tests) -- this test only cares
        # that a *complete* enrollment with a certificate already issued
        # links to course_detail, not about certificate generation itself.
        Certificate.objects.create(enrollment=enrollment)
        response = self.client.get(reverse('my_learning'))
        self.assertContains(response, reverse('course_detail', args=[self.course.id]))


class HomeworkSubmissionTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='hw_inst', password='pw', is_instructor=True)
        self.other_instructor = User.objects.create_user(
            username='hw_inst2', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='hw_stud', password='pw', is_student=True)
        self.outsider = User.objects.create_user(
            username='hw_outsider', password='pw', is_student=True)
        track = Track.objects.create(name='HW Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='HW Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=self.course, title='M1')
        self.hw_lecture = Lecture.objects.create(
            module=module, title='Assignment 1', accepts_submission=True)
        self.plain_lecture = Lecture.objects.create(
            module=module, title='No Homework Here', accepts_submission=False)
        Enrollment.objects.create(student=self.student, course=self.course)

    def _submit_url(self, lecture):
        return reverse('submit_homework', args=[self.course.id, lecture.id])

    def test_enrolled_student_can_submit_homework(self):
        self.client.force_login(self.student)
        response = self.client.post(self._submit_url(self.hw_lecture), {
            'submission_link': 'https://github.com/example/repo', 'note': 'done',
        })
        self.assertEqual(response.status_code, 302)
        submission = Submission.objects.get(student=self.student, lecture=self.hw_lecture)
        self.assertEqual(submission.submission_link, 'https://github.com/example/repo')
        self.assertIsNone(submission.graded_at)

    def test_cannot_submit_to_lecture_that_does_not_accept_submission(self):
        self.client.force_login(self.student)
        response = self.client.post(self._submit_url(self.plain_lecture), {
            'note': 'sneaky',
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Submission.objects.filter(lecture=self.plain_lecture).exists())

    def test_unenrolled_student_cannot_submit_homework(self):
        self.client.force_login(self.outsider)
        response = self.client.post(self._submit_url(self.hw_lecture), {'note': 'x'})
        self.assertEqual(response.status_code, 404)

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(self._submit_url(self.hw_lecture))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_student_can_update_ungraded_submission(self):
        self.client.force_login(self.student)
        self.client.post(self._submit_url(self.hw_lecture), {'note': 'first draft'})
        self.client.post(self._submit_url(self.hw_lecture), {'note': 'final draft'})
        self.assertEqual(Submission.objects.filter(student=self.student, lecture=self.hw_lecture).count(), 1)
        submission = Submission.objects.get(student=self.student, lecture=self.hw_lecture)
        self.assertEqual(submission.note, 'final draft')

    def test_graded_submission_is_locked_against_further_edits(self):
        submission = Submission.objects.create(
            student=self.student, lecture=self.hw_lecture, note='original',
            grade='90', graded_at=timezone.now())
        self.client.force_login(self.student)
        response = self.client.post(self._submit_url(self.hw_lecture), {'note': 'trying to sneak an edit in'})
        self.assertEqual(response.status_code, 200)
        submission.refresh_from_db()
        self.assertEqual(submission.note, 'original')


class GradeSubmissionTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='gr_inst', password='pw', is_instructor=True)
        self.other_instructor = User.objects.create_user(
            username='gr_inst2', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='gr_stud', password='pw', is_student=True)
        track = Track.objects.create(name='Grade Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Grade Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=self.course, title='M1')
        self.lecture = Lecture.objects.create(module=module, title='Assignment', accepts_submission=True)
        self.submission = Submission.objects.create(
            student=self.student, lecture=self.lecture, note='here is my work')

    def _grade_url(self):
        return reverse('grade_submission', args=[self.submission.id])

    def test_instructor_can_grade_own_course_submission(self):
        self.client.force_login(self.instructor)
        response = self.client.post(self._grade_url(), {'grade': '95', 'feedback': 'Great work!'})
        self.assertEqual(response.status_code, 302)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.grade, '95')
        self.assertEqual(self.submission.feedback, 'Great work!')
        self.assertIsNotNone(self.submission.graded_at)

    def test_other_instructor_cannot_grade_submission_for_someone_elses_course(self):
        self.client.force_login(self.other_instructor)
        response = self.client.post(self._grade_url(), {'grade': '10', 'feedback': 'nope'})
        self.assertEqual(response.status_code, 404)
        self.submission.refresh_from_db()
        self.assertIsNone(self.submission.grade)
        self.assertIsNone(self.submission.graded_at)

    def test_cannot_regrade_an_already_graded_submission(self):
        self.submission.grade = '80'
        self.submission.graded_at = timezone.now()
        self.submission.save()
        self.client.force_login(self.instructor)
        response = self.client.post(self._grade_url(), {'grade': '100', 'feedback': 'changed my mind'})
        self.assertEqual(response.status_code, 403)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.grade, '80')

    def test_other_instructor_cannot_view_course_submissions(self):
        self.client.force_login(self.other_instructor)
        response = self.client.get(reverse('course_submissions', args=[self.course.id]))
        self.assertEqual(response.status_code, 404)

    def test_owning_instructor_can_view_course_submissions(self):
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('course_submissions', args=[self.course.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'gr_stud')


class PasswordResetFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='pw_reset_user', password='oldpassword123', email='reset@example.com')

    def _confirm_url(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return reverse('password_reset_confirm', args=[uidb64, token]), uidb64, token

    def test_reset_form_page_loads(self):
        response = self.client.get(reverse('password_reset'))
        self.assertEqual(response.status_code, 200)

    def test_valid_email_sends_reset_email(self):
        response = self.client.post(reverse('password_reset'), {'email': 'reset@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Reset your Mendoura password')
        self.assertIn('reset@example.com', mail.outbox[0].to)
        # Multipart: plain-text body plus an HTML alternative.
        self.assertTrue(any(content_type == 'text/html' for _, content_type in mail.outbox[0].alternatives))

    def test_reset_email_states_expiry_derived_from_settings(self):
        self.client.post(reverse('password_reset'), {'email': 'reset@example.com'})
        body = mail.outbox[0].body
        expected = emails.humanize_duration(settings.PASSWORD_RESET_TIMEOUT)
        self.assertIn(f'expire in {expected}', body)

    def test_student_account_gets_student_reset_template(self):
        response = self.client.post(reverse('password_reset'), {'email': 'reset@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(mail.outbox[0].subject, 'Reset your Mendoura password')
        self.assertNotIn('instructor account', mail.outbox[0].body)

    def test_instructor_account_gets_instructor_reset_template(self):
        User.objects.create_user(
            username='pw_reset_inst', password='oldpassword123', email='reset_inst@example.com',
            is_instructor=True)
        response = self.client.post(reverse('password_reset'), {'email': 'reset_inst@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(mail.outbox[0].subject, 'Reset your Mendoura instructor password')
        self.assertIn('instructor account', mail.outbox[0].body)
        self.assertTrue(any(content_type == 'text/html' for _, content_type in mail.outbox[0].alternatives))

    def test_unknown_email_does_not_leak_account_existence(self):
        response = self.client.post(reverse('password_reset'), {'email': 'nobody@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_token_allows_setting_new_password(self):
        url, uidb64, token = self._confirm_url(self.user)
        # First GET redirects to the token-consumed session-keyed URL Django's view uses.
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set a new password')

        response = self.client.post(response.request['PATH_INFO'], {
            'new_password1': 'brandnewpassword456',
            'new_password2': 'brandnewpassword456',
        })
        self.assertRedirects(response, reverse('password_reset_complete'))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('brandnewpassword456'))

    def test_invalid_token_shows_expired_message(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        url = reverse('password_reset_confirm', args=[uidb64, 'bogus-token'])
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Link expired')

    def test_reused_token_cannot_reset_password_twice(self):
        url, uidb64, token = self._confirm_url(self.user)
        response = self.client.get(url, follow=True)
        set_password_url = response.request['PATH_INFO']
        self.client.post(set_password_url, {
            'new_password1': 'firstnewpassword789',
            'new_password2': 'firstnewpassword789',
        })
        # Reusing the original emailed link a second time must not work --
        # the token was already consumed by the first successful reset.
        response = self.client.get(url, follow=True)
        self.assertContains(response, 'Link expired')


class AICoachTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(username='ai_stud', password='pw', is_student=True)
        self.instructor = User.objects.create_user(
            username='ai_inst', password='pw', is_instructor=True)

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(reverse('ai_coach'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_non_student_cannot_view_page(self):
        self.client.force_login(self.instructor)
        response = self.client.get(reverse('ai_coach'))
        self.assertRedirects(response, reverse('platform_home'))

    def test_student_sees_greeting_on_first_visit(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('ai_coach'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Welcome to Mendoura AI Coach')
        self.assertTrue(AIConversation.objects.filter(student=self.student).exists())

    def test_page_shows_sandbox_badge_when_api_key_missing(self):
        self.client.force_login(self.student)
        with override_settings(GEMINI_API_KEY=''):
            response = self.client.get(reverse('ai_coach'))
        self.assertContains(response, 'Mendoura General AI Coach')

    def test_no_sandbox_badge_when_api_key_configured(self):
        self.client.force_login(self.student)
        with override_settings(GEMINI_API_KEY='test-key'):
            response = self.client.get(reverse('ai_coach'))
        self.assertNotContains(response, 'Mendoura General AI Coach')

    def test_non_student_cannot_post_message(self):
        self.client.force_login(self.instructor)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'hi'}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_get_not_allowed_on_send_endpoint(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('ai_coach_send'))
        self.assertEqual(response.status_code, 405)

    def test_empty_message_rejected(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': '   '}), content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_overlong_message_rejected(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'x' * 6001}),
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    @patch('courses.views.ai_coach_client.send_message')
    def test_successful_reply_persists_both_messages_and_renders_markdown(self, mock_send):
        mock_send.return_value = 'Hello **world**'
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Hi coach'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('<strong>world</strong>', response.json()['reply_html'])

        conversation = AIConversation.objects.get(student=self.student)
        messages = list(conversation.messages.order_by('created_at'))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, AIMessage.Role.USER)
        self.assertEqual(messages[0].content, 'Hi coach')
        self.assertEqual(messages[1].role, AIMessage.Role.ASSISTANT)
        self.assertEqual(messages[1].content, 'Hello **world**')

    @patch('courses.views.ai_coach_client.send_message')
    def test_api_error_returns_502_and_does_not_store_assistant_reply(self, mock_send):
        mock_send.side_effect = ai_coach.AICoachError('Simulated API failure.')
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Hi coach'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 502)

        conversation = AIConversation.objects.get(student=self.student)
        # The student's message is kept even though the reply failed --
        # only the assistant side is missing.
        self.assertEqual(conversation.messages.count(), 1)
        self.assertEqual(conversation.messages.first().role, AIMessage.Role.USER)

    @patch('courses.views.ai_coach_client.send_message')
    def test_history_is_replayed_oldest_first(self, mock_send):
        mock_send.return_value = 'ok'
        self.client.force_login(self.student)
        self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'first'}),
            content_type='application/json')
        self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'second'}),
            content_type='application/json')

        second_call_history = mock_send.call_args_list[1].args[0]
        contents = [m['content'] for m in second_call_history]
        self.assertEqual(contents, ['first', 'ok', 'second'])

    @patch('courses.views.ai_coach_client.send_message')
    def test_existing_conversation_is_reused_across_requests(self, mock_send):
        mock_send.return_value = 'ok'
        self.client.force_login(self.student)
        self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'first'}),
            content_type='application/json')
        self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'second'}),
            content_type='application/json')
        self.assertEqual(AIConversation.objects.filter(student=self.student).count(), 1)

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_returns_200_with_sandbox_reply(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Hey there'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Mendoura General AI Assistant', response.json()['reply_html'])

        conversation = AIConversation.objects.get(student=self.student)
        messages = list(conversation.messages.order_by('created_at'))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1].role, AIMessage.Role.ASSISTANT)

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_tech_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you help me learn Python?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Modern Software Engineering', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_business_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'How do I grow my business marketing?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Elite Entrepreneurship Framework', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_language_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'I want to learn Arabic'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Language Learning Roadmap', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_study_schedule_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you build me a study schedule?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Weekly Study Schedule', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_math_science_keyword_in_arabic(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'عايز افهم فيزياء'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Math &amp; Science', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_career_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you help me prep for a job interview?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Career &amp; Interview Prep Kit', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_design_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'How do I get better at Figma and UX design?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Design Fundamentals', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_matches_productivity_keyword_in_arabic(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'محتاج تحفيز وتنظيم وقتي'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Focus &amp; Productivity Framework', response.json()['reply_html'])

    @override_settings(GEMINI_API_KEY='')
    def test_send_without_ai_configured_unmatched_query_gets_dynamic_catch_all(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'),
            data=json.dumps({'message': 'What is the meaning of life anyway?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('How to Structurally Analyze Any Topic', response.json()['reply_html'])
        self.assertIn('meaning of life', response.json()['reply_html'])


class AICoachClientTests(TestCase):
    def test_is_configured_reflects_setting(self):
        with override_settings(GEMINI_API_KEY=''):
            self.assertFalse(ai_coach.is_configured())
        with override_settings(GEMINI_API_KEY='some-key'):
            self.assertTrue(ai_coach.is_configured())

    def test_send_message_returns_sandbox_reply_when_not_configured(self):
        with override_settings(GEMINI_API_KEY=''):
            reply = ai_coach.send_message([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(reply, ai_coach._catch_all_reply('hi'))

    def test_sandbox_reply_matches_tech_keywords(self):
        for keyword in ('python', 'js', 'javascript', 'html', 'code', 'bug', 'web'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_TECH_GUIDE)

    def test_sandbox_reply_matches_business_keywords(self):
        for keyword in ('marketing', 'business', 'sales', 'profit', 'project'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_BUSINESS_FRAMEWORK)

    def test_sandbox_reply_matches_language_keywords(self):
        for keyword in ('english', 'arabic', 'translation', 'learn'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_LANGUAGE_ROADMAP)

    def test_sandbox_reply_matches_study_keywords(self):
        for keyword in ('study', 'schedule', 'exam'):
            history = [{'role': 'user', 'content': f'Help me with my {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_STUDY_SCHEDULE)

    def test_sandbox_reply_matches_math_science_keywords(self):
        for keyword in ('math', 'physics', 'science', 'calculus', 'equation', 'رياضيات', 'فيزياء', 'علوم'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_MATH_SCIENCE_GUIDE)

    def test_sandbox_reply_matches_career_keywords(self):
        for keyword in ('job', 'resume', 'interview', 'career', 'cv', 'وظيفة', 'مقابلة', 'سيرة ذاتية'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_CAREER_GUIDE)

    def test_sandbox_reply_matches_design_keywords(self):
        for keyword in ('ui', 'ux', 'design', 'photoshop', 'figma', 'colors', 'تصميم', 'فوتوشوب'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_DESIGN_GUIDE)

    def test_sandbox_reply_matches_productivity_keywords(self):
        for keyword in ('focus', 'time management', 'motivation', 'تركيز', 'وقت', 'تنظيم', 'تحفيز'):
            history = [{'role': 'user', 'content': f'Tell me about {keyword}'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_PRODUCTIVITY_GUIDE)

    def test_design_ui_keyword_does_not_false_positive_on_substring(self):
        # "build" contains the letters "ui" -- must not be misread as the
        # design keyword "ui" thanks to word-boundary matching.
        history = [{'role': 'user', 'content': 'Can you build me a study schedule?'}]
        self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_STUDY_SCHEDULE)

    def test_sandbox_reply_matches_general_chitchat_keywords(self):
        for keyword in ('hi', 'hello', 'help', 'explain', 'how to', 'why', 'what is'):
            history = [{'role': 'user', 'content': f'{keyword} there'}]
            self.assertEqual(ai_coach._sandbox_reply(history), ai_coach._catch_all_reply(f'{keyword} there'))

    def test_sandbox_reply_falls_back_to_general_for_unmatched_text(self):
        history = [{'role': 'user', 'content': 'asdfghjkl'}]
        self.assertEqual(ai_coach._sandbox_reply(history), ai_coach._catch_all_reply('asdfghjkl'))

    def test_sandbox_reply_uses_most_recent_user_message(self):
        history = [
            {'role': 'user', 'content': 'python please'},
            {'role': 'assistant', 'content': '...'},
            {'role': 'user', 'content': 'actually, build me a study schedule'},
        ]
        self.assertEqual(ai_coach._sandbox_reply(history), ai_coach.SANDBOX_STUDY_SCHEDULE)

    def test_catch_all_reply_restates_the_users_query(self):
        reply = ai_coach._catch_all_reply('How do black holes actually form?')
        self.assertIn('How do black holes actually form?', reply)
        self.assertIn('How to Structurally Analyze Any Topic', reply)
        self.assertIn('| Step | Focus | What To Do |', reply)

    def test_catch_all_reply_prefix_is_chosen_deterministically_by_input_length(self):
        text = 'x' * 7
        expected_prefix = ai_coach.CATCH_ALL_PREFIXES[len(text) % len(ai_coach.CATCH_ALL_PREFIXES)]
        self.assertIn(expected_prefix, ai_coach._catch_all_reply(text))

        other_text = 'x' * 9
        other_prefix = ai_coach.CATCH_ALL_PREFIXES[len(other_text) % len(ai_coach.CATCH_ALL_PREFIXES)]
        self.assertNotEqual(expected_prefix, other_prefix)
        self.assertIn(other_prefix, ai_coach._catch_all_reply(other_text))

    def test_catch_all_reply_is_deterministic_for_the_same_input(self):
        self.assertEqual(
            ai_coach._catch_all_reply('what should I learn next'),
            ai_coach._catch_all_reply('what should I learn next'),
        )

    def test_catch_all_reply_truncates_very_long_queries(self):
        long_query = 'why ' * 50
        reply = ai_coach._catch_all_reply(long_query)
        self.assertIn('...', reply)


@override_settings(GEMINI_API_KEY='test-gemini-key')
class GeminiBackendTests(TestCase):
    """send_message's real (non-sandbox) path -- the Anthropic-to-Gemini
    swap. The one network call (genai.Client) is mocked, same pattern as
    bunny.create_video's requests.post."""

    def setUp(self):
        cache.clear()

    @patch('courses.ai_coach.genai.Client')
    def test_send_message_returns_gemini_text(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='Hello from Gemini')
        mock_client_cls.return_value = mock_client

        reply = ai_coach.send_message([{'role': 'user', 'content': 'hi'}], user_id=1)
        self.assertEqual(reply, 'Hello from Gemini')
        mock_client_cls.assert_called_once_with(api_key='test-gemini-key')

    @patch('courses.ai_coach.genai.Client')
    def test_assistant_role_mapped_to_model_for_gemini(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='ok')
        mock_client_cls.return_value = mock_client

        ai_coach.send_message(
            [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'prior reply'}],
            user_id=1)

        contents = mock_client.models.generate_content.call_args.kwargs['contents']
        self.assertEqual(contents[0].role, 'user')
        self.assertEqual(contents[1].role, 'model')

    @patch('courses.ai_coach.genai.Client')
    def test_lesson_context_appended_to_system_instruction(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='ok')
        mock_client_cls.return_value = mock_client

        ai_coach.send_message(
            [{'role': 'user', 'content': 'brief this lesson'}], user_id=1,
            context='Lesson: Intro to Loops\nTranscript: """for loops repeat code"""')

        config = mock_client.models.generate_content.call_args.kwargs['config']
        self.assertIn('Intro to Loops', config.system_instruction)
        self.assertIn('Never invent or guess', config.system_instruction)

    @patch('courses.ai_coach.genai.Client')
    def test_auth_or_other_api_error_raises_generic_friendly_message(self, mock_client_cls):
        from google.genai import errors as genai_errors
        mock_client = Mock()
        mock_client.models.generate_content.side_effect = genai_errors.APIError(
            401, {'error': {'message': 'API key not valid'}})
        mock_client_cls.return_value = mock_client

        with self.assertRaises(ai_coach.AICoachError) as ctx:
            ai_coach.send_message([{'role': 'user', 'content': 'hi'}], user_id=1)
        # The student never sees Google's raw error text -- just a friendly message.
        self.assertNotIn('API key not valid', str(ctx.exception))
        self.assertIn("couldn't reach the AI service", str(ctx.exception))

    @patch('courses.ai_coach.genai.Client')
    def test_gemini_429_raises_free_tier_limit_specific_message(self, mock_client_cls):
        from google.genai import errors as genai_errors
        mock_client = Mock()
        mock_client.models.generate_content.side_effect = genai_errors.APIError(
            429, {'error': {'message': 'quota exceeded'}})
        mock_client_cls.return_value = mock_client

        with self.assertRaises(ai_coach.AICoachError) as ctx:
            ai_coach.send_message([{'role': 'user', 'content': 'hi'}], user_id=1)
        self.assertNotIn('quota exceeded', str(ctx.exception))
        self.assertIn("free-tier limit", str(ctx.exception))

    @patch('courses.ai_coach.genai.Client')
    def test_empty_gemini_response_raises_ai_coach_error(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='')
        mock_client_cls.return_value = mock_client

        with self.assertRaises(ai_coach.AICoachError):
            ai_coach.send_message([{'role': 'user', 'content': 'hi'}], user_id=1)


@override_settings(
    GEMINI_API_KEY='test-gemini-key',
    GEMINI_RATE_LIMIT_PER_MINUTE=2, GEMINI_RATE_LIMIT_PER_DAY=100,
    GEMINI_USER_RATE_LIMIT_PER_MINUTE=1, GEMINI_USER_RATE_LIMIT_PER_DAY=100)
class AICoachRateLimitTests(TestCase):
    """Basic per-user and shared project-wide throttling, sized to stay
    within Gemini's free tier -- Google enforces its quota against the whole
    API key, not per Mendoura student, so both buckets are tested."""

    def setUp(self):
        cache.clear()

    @patch('courses.ai_coach.genai.Client')
    def test_user_over_their_own_limit_is_blocked(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='ok')
        mock_client_cls.return_value = mock_client

        ai_coach.send_message([{'role': 'user', 'content': 'one'}], user_id=42)
        with self.assertRaises(ai_coach.AICoachError):
            ai_coach.send_message([{'role': 'user', 'content': 'two'}], user_id=42)

    @patch('courses.ai_coach.genai.Client')
    def test_different_users_share_the_global_budget(self, mock_client_cls):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = Mock(text='ok')
        mock_client_cls.return_value = mock_client

        # Global cap is 2/minute; each of these 2 users is within their own
        # (1/minute) budget, but together they exhaust the shared ceiling.
        ai_coach.send_message([{'role': 'user', 'content': 'one'}], user_id=1)
        ai_coach.send_message([{'role': 'user', 'content': 'two'}], user_id=2)
        with self.assertRaises(ai_coach.AICoachError):
            ai_coach.send_message([{'role': 'user', 'content': 'three'}], user_id=3)

    @patch('courses.ai_coach.genai.Client')
    def test_sandbox_mode_is_never_rate_limited(self, mock_client_cls):
        # Rate limiting only matters once real API calls cost quota --
        # sandbox mode never calls Gemini at all.
        with override_settings(GEMINI_API_KEY=''):
            for _ in range(5):
                ai_coach.send_message([{'role': 'user', 'content': 'hi'}], user_id=42)
        mock_client_cls.assert_not_called()


class AICoachWidgetHistoryTests(TestCase):
    """GET endpoint the floating AI Coach widget uses to preload the same
    persisted conversation the full /dashboard/ai-coach/ page shows."""

    def setUp(self):
        self.student = User.objects.create_user(username='wh_stud', password='pw', is_student=True)

    def test_non_student_forbidden(self):
        instructor = User.objects.create_user(username='wh_inst', password='pw', is_instructor=True)
        self.client.force_login(instructor)
        response = self.client.get(reverse('ai_coach_widget_history'))
        self.assertEqual(response.status_code, 403)

    def test_empty_history_when_no_conversation_yet(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('ai_coach_widget_history'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['messages'], [])

    def test_returns_persisted_conversation_markdown_rendered(self):
        conversation = AIConversation.objects.create(student=self.student)
        AIMessage.objects.create(conversation=conversation, role=AIMessage.Role.USER, content='hi')
        AIMessage.objects.create(
            conversation=conversation, role=AIMessage.Role.ASSISTANT, content='hello **there**')
        self.client.force_login(self.student)
        messages = self.client.get(reverse('ai_coach_widget_history')).json()['messages']
        self.assertEqual(len(messages), 2)
        self.assertIn('<strong>there</strong>', messages[1]['html'])
        self.assertIsNone(messages[0]['html'])


class AICoachLessonSendTests(TestCase):
    """The lesson-embedded AI Coach: grounded in the current lesson/module's
    actual content instead of general chat, and deliberately ephemeral (no
    AIConversation/AIMessage row) since it's a per-lesson Q&A, not the
    student's one long-running study thread."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='als_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(username='als_stud', password='pw', is_student=True)
        self.outsider = User.objects.create_user(username='als_out', password='pw', is_student=True)
        track = Track.objects.create(name='ALS Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='ALS Course', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        self.module = Module.objects.create(course=self.course, title='M1')
        self.lecture_with_script = Lecture.objects.create(
            module=self.module, title='Scripted Lesson',
            ai_generated_script='This lesson covers loops.', order=1)
        self.lecture_without_script = Lecture.objects.create(
            module=self.module, title='Unscripted Lesson', order=2)
        Enrollment.objects.create(student=self.student, course=self.course)

    def _url(self, lecture):
        return reverse('ai_coach_lesson_send', args=[self.course.id, lecture.id])

    def _post(self, lecture, message, history=None):
        body = {'message': message}
        if history is not None:
            body['history'] = history
        return self.client.post(self._url(lecture), data=json.dumps(body), content_type='application/json')

    def test_unenrolled_non_preview_forbidden(self):
        self.client.force_login(self.outsider)
        response = self._post(self.lecture_with_script, 'brief this lesson')
        self.assertEqual(response.status_code, 403)

    def test_instructor_cannot_use_student_endpoint(self):
        self.client.force_login(self.instructor)
        response = self._post(self.lecture_with_script, 'brief this lesson')
        self.assertEqual(response.status_code, 403)

    def test_preview_lecture_accessible_without_enrollment(self):
        preview_lecture = Lecture.objects.create(module=self.module, title='Preview', is_preview=True, order=3)
        self.client.force_login(self.outsider)
        with patch('courses.views.ai_coach_client.send_message', return_value='ok'):
            response = self._post(preview_lecture, 'hi')
        self.assertEqual(response.status_code, 200)

    def test_empty_message_rejected(self):
        self.client.force_login(self.student)
        response = self._post(self.lecture_with_script, '   ')
        self.assertEqual(response.status_code, 400)

    @patch('courses.views.ai_coach_client.send_message')
    def test_enrolled_student_gets_lesson_context_grounded_reply(self, mock_send):
        mock_send.return_value = 'This lesson is about loops.'
        self.client.force_login(self.student)
        response = self._post(self.lecture_with_script, 'brief this lesson')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply'], 'This lesson is about loops.')
        context_kwarg = mock_send.call_args.kwargs['context']
        self.assertIn('This lesson covers loops.', context_kwarg)
        self.assertIn('Scripted Lesson', context_kwarg)

    @patch('courses.views.ai_coach_client.send_message')
    def test_context_notes_missing_transcript_for_unscripted_lesson(self, mock_send):
        mock_send.return_value = 'ok'
        self.client.force_login(self.student)
        self._post(self.lecture_without_script, 'brief this lesson')
        context_kwarg = mock_send.call_args.kwargs['context']
        self.assertIn('Unscripted Lesson', context_kwarg)
        self.assertIn('not available', context_kwarg)

    @patch('courses.views.ai_coach_client.send_message')
    def test_full_production_course_has_no_transcript_for_any_lesson(self, mock_send):
        mock_send.return_value = 'ok'
        full_course = Course.objects.create(
            instructor=self.instructor, track=self.course.track, title='Full Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED)
        module = Module.objects.create(course=full_course, title='M1')
        # Even if ai_generated_script somehow has stray text, FULL production
        # never surfaces it as a transcript -- it's not real lesson text.
        lecture = Lecture.objects.create(module=module, title='Full Lesson', ai_generated_script='stray text')
        Enrollment.objects.create(student=self.student, course=full_course)
        self.client.force_login(self.student)
        self.client.post(
            reverse('ai_coach_lesson_send', args=[full_course.id, lecture.id]),
            data=json.dumps({'message': 'brief this lesson'}), content_type='application/json')
        context_kwarg = mock_send.call_args.kwargs['context']
        self.assertIn('not available', context_kwarg)
        self.assertNotIn('stray text', context_kwarg)

    @patch('courses.views.ai_coach_client.send_message')
    def test_does_not_persist_to_ai_conversation(self, mock_send):
        mock_send.return_value = 'ok'
        self.client.force_login(self.student)
        self._post(self.lecture_with_script, 'brief this lesson')
        self.assertFalse(AIConversation.objects.filter(student=self.student).exists())

    @patch('courses.views.ai_coach_client.send_message')
    def test_client_held_history_is_resent(self, mock_send):
        mock_send.return_value = 'ok'
        self.client.force_login(self.student)
        history = [{'role': 'user', 'content': 'earlier q'}, {'role': 'assistant', 'content': 'earlier a'}]
        self._post(self.lecture_with_script, 'and now?', history=history)
        sent_history = mock_send.call_args.args[0]
        self.assertEqual(sent_history[0]['content'], 'earlier q')
        self.assertEqual(sent_history[-1]['content'], 'and now?')

    @patch('courses.views.ai_coach_client.send_message')
    def test_api_error_returns_502_with_friendly_message(self, mock_send):
        mock_send.side_effect = ai_coach.AICoachError('Friendly message.')
        self.client.force_login(self.student)
        response = self._post(self.lecture_with_script, 'brief this lesson')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'Friendly message.')


class AutoTranslateClientTests(TestCase):
    """auto_translate wraps deep-translator's free GoogleTranslator -- no
    API key, no billing. These tests mock GoogleTranslator.translate itself
    (the one network call) rather than hitting the real endpoint."""

    def test_is_configured_reflects_setting(self):
        with override_settings(AUTO_TRANSLATE_ENABLED=True):
            self.assertTrue(auto_translate.is_configured())
        with override_settings(AUTO_TRANSLATE_ENABLED=False):
            self.assertFalse(auto_translate.is_configured())

    def test_translate_fields_returns_empty_for_no_fields_or_languages(self):
        self.assertEqual(auto_translate.translate_fields({}, ['ar']), {})
        self.assertEqual(auto_translate.translate_fields({'name': 'Robotics'}, []), {})

    @patch('courses.auto_translate.GoogleTranslator.translate')
    def test_translate_fields_translates_plain_text(self, mock_translate):
        mock_translate.return_value = 'الروبوتات'
        result = auto_translate.translate_fields({'name': 'Robotics'}, ['ar'])
        self.assertEqual(result, {'name': {'ar': 'الروبوتات'}})

    @patch('courses.auto_translate.GoogleTranslator.translate')
    def test_translate_fields_raises_translation_error_on_failure(self, mock_translate):
        mock_translate.side_effect = Exception('rate limited')
        with self.assertRaises(auto_translate.TranslationError):
            auto_translate.translate_fields({'name': 'Robotics'}, ['ar'])

    @patch('courses.auto_translate.GoogleTranslator.translate')
    def test_translate_fields_wraps_a_raw_network_error_from_requests(self, mock_translate):
        """deep-translator only wraps its own known failure modes -- a raw
        connection/proxy error from the underlying HTTP library must still
        come out as TranslationError, not crash the caller."""
        import requests
        mock_translate.side_effect = requests.exceptions.ProxyError('tunnel failed')
        with self.assertRaises(auto_translate.TranslationError):
            auto_translate.translate_fields({'name': 'Robotics'}, ['ar'])

    @patch('courses.auto_translate.GoogleTranslator.translate')
    def test_markdown_table_structure_survives_translation(self, mock_translate):
        mock_translate.side_effect = lambda text: f'[AR] {text}'
        body = (
            "Here's a table:\n\n"
            "| Scenario | Share |\n"
            "|---|---|\n"
            "| **Monthly** | 60% |\n"
        )
        translated = auto_translate.translate_markdown(body, 'ar')
        lines = translated.split('\n')
        # Separator row is untouched -- nothing human-readable in it.
        self.assertIn('|---|---|', lines)
        # Header and data rows keep exactly 2 columns, each cell translated
        # independently (never the pipe characters themselves).
        header_cells = [c.strip() for c in lines[2].split('|') if c.strip()]
        self.assertEqual(header_cells, ['[AR] Scenario', '[AR] Share'])
        data_cells = [c.strip() for c in lines[4].split('|') if c.strip()]
        self.assertEqual(data_cells, ['[AR] **Monthly**', '[AR] 60%'])

    @patch('courses.auto_translate.GoogleTranslator.translate')
    def test_bullet_list_lines_translated_individually(self, mock_translate):
        mock_translate.side_effect = lambda text: f'[AR] {text}'
        body = '- First point\n- Second point'
        translated = auto_translate.translate_markdown(body, 'ar')
        self.assertEqual(translated, '- [AR] First point\n- [AR] Second point')


class LegalDocumentTests(TestCase):
    def setUp(self):
        # Most of this class's tests only care that the English content
        # exists and renders -- keep translation off for the seed itself so
        # they don't make real network calls; the two tests that actually
        # exercise translation turn it back on themselves.
        with override_settings(AUTO_TRANSLATE_ENABLED=False):
            call_command('seed_legal_docs')
        self.terms = LegalDocument.objects.get(slug='terms')
        self.privacy = LegalDocument.objects.get(slug='privacy')

    def test_terms_page_renders_sections_and_table(self):
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Instructor Compensation')
        self.assertContains(response, '<table>')
        self.assertContains(response, '60%')
        self.assertContains(response, 'id="revenue-share"')
        self.assertContains(response, 'id="taxes"')

    def test_privacy_page_renders_sections_and_table(self):
        response = self.client.get(reverse('privacy'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Data We Collect')
        self.assertContains(response, '<table>')

    def test_privacy_page_does_not_name_infrastructure_vendors(self):
        response = self.client.get(reverse('privacy'))
        content = response.content.decode()
        self.assertNotIn('Render', content)
        self.assertNotIn('Cloudinary', content)

    def test_footer_links_to_terms_and_privacy_on_every_page(self):
        response = self.client.get(reverse('platform_home'))
        self.assertContains(response, reverse('terms'))
        self.assertContains(response, reverse('privacy'))
        self.assertContains(response, 'support@mendoura.com')

    @override_settings(AUTO_TRANSLATE_ENABLED=True)
    def test_language_switch_renders_translated_content_via_ai_pipeline(self):
        """Same mechanism Track uses: no hardcoded per-language template
        text -- translated_heading/translated_body come from the AI
        pipeline's stored JSON, keyed by the active language."""
        def fake_translate_fields(fields, target_languages):
            return {field: {lang: f'[{lang.upper()}] {text}' for lang in target_languages}
                    for field, text in fields.items()}

        section = self.terms.sections.get(anchor='revenue-share')
        with patch('courses.auto_translate.translate_fields', side_effect=fake_translate_fields):
            section.save()

        with translation_override('ar'):
            self.assertIn('[AR]', section.translated_heading)
            self.assertIn('<table>', section.body_html)

        # Exercise the real language-switch path (Accept-Language, same as
        # LocaleMiddleware honors after the nav switcher sets its cookie),
        # not just the translation_override() context manager directly.
        response = self.client.get(reverse('terms'), HTTP_ACCEPT_LANGUAGE='ar')
        self.assertEqual(response.wsgi_request.LANGUAGE_CODE, 'ar')
        self.assertContains(response, '[AR]')

        with translation_override('en'):
            self.assertNotIn('[AR]', section.translated_heading)

    def test_seed_legal_docs_is_idempotent(self):
        section_count_before = LegalSection.objects.count()
        call_command('seed_legal_docs')
        self.assertEqual(LegalSection.objects.count(), section_count_before)

    def test_table_still_renders_when_translation_prefixes_the_body(self):
        """python-markdown's table extension only recognizes a table as the
        very first line of a block -- any leading text (e.g. a translation
        that prepends a stray word) silently degrades it to plain text
        instead of <table>. Regression test for the "Data We Collect"
        section, whose body used to start directly with the table; it now
        has a lead-in sentence, same as "Instructor Revenue Share" already
        did, specifically so a translated prefix can't land on the same
        line as the table header."""
        section = self.privacy.sections.get(anchor='data-we-collect')

        def fake_translate_fields(fields, target_languages):
            return {field: {lang: f'[{lang.upper()}] {text}' for lang in target_languages}
                    for field, text in fields.items()}

        with override_settings(AUTO_TRANSLATE_ENABLED=True), \
                patch('courses.auto_translate.translate_fields', side_effect=fake_translate_fields):
            section.save()

        with translation_override('ar'):
            self.assertIn('<table>', section.body_html)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class QuizModelTests(TestCase):
    """Model-level behavior: module completion gating, best-score
    aggregation, attempt numbering, and the attempt-3+ answer reveal."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='quiz_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='quiz_stud', password='pw', is_student=True, email='quiz@example.com')
        track = Track.objects.create(name='Quiz Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Quiz Course', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, price=Decimal('0.00'),
            is_free=True, status=Course.Status.PUBLISHED,
        )
        self.module = Module.objects.create(course=self.course, title='Module 1')
        self.lecture = Lecture.objects.create(module=self.module, title='Only Lecture')
        self.quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.q1 = Question.objects.create(quiz=self.quiz, text='2+2?', order=1)
        self.c1_correct = Choice.objects.create(question=self.q1, text='4', is_correct=True)
        self.c1_wrong = Choice.objects.create(question=self.q1, text='5', is_correct=False)
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def _complete_lecture(self):
        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))

    def _take_quiz(self, correct):
        choice = self.c1_correct if correct else self.c1_wrong
        return self.client.post(reverse('take_quiz', args=[self.course.id, self.module.id]), {
            f'question_{self.q1.id}': choice.id,
        })

    def test_module_with_quiz_is_not_complete_until_quiz_passed(self):
        self._complete_lecture()
        self.enrollment.refresh_from_db()
        self.assertFalse(self.enrollment.module_is_complete(self.module))
        self.assertFalse(self.enrollment.is_complete())

    def test_module_completes_once_quiz_is_passed(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=True)
        self.assertTrue(self.enrollment.module_is_complete(self.module))
        self.assertTrue(self.enrollment.is_complete())

    def test_failing_quiz_does_not_complete_module_or_issue_certificate(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=False)
        self.assertFalse(self.enrollment.module_is_complete(self.module))
        self.assertFalse(Certificate.objects.filter(enrollment=self.enrollment).exists())

    def test_passing_quiz_issues_certificate(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=True)
        self.assertTrue(Certificate.objects.filter(enrollment=self.enrollment).exists())

    def test_quiz_with_no_questions_does_not_gate_completion(self):
        empty_module = Module.objects.create(course=self.course, title='Module 2')
        empty_lecture = Lecture.objects.create(module=empty_module, title='L2')
        Quiz.objects.create(module=empty_module, passing_score_percent=70)  # zero questions

        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, empty_lecture.id]))
        self._take_quiz(correct=True)  # pass the one real quiz

        self.enrollment.refresh_from_db()
        self.assertTrue(self.enrollment.module_is_complete(empty_module))
        self.assertTrue(self.enrollment.is_complete())

    def test_unlimited_retakes_allowed_and_attempt_number_increments(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=False)
        self._take_quiz(correct=False)
        self._take_quiz(correct=True)

        attempts = list(QuizAttempt.objects.filter(enrollment=self.enrollment, quiz=self.quiz)
                         .order_by('submitted_at'))
        self.assertEqual([a.attempt_number for a in attempts], [1, 2, 3])
        self.assertFalse(attempts[0].passed)
        self.assertFalse(attempts[1].passed)
        self.assertTrue(attempts[2].passed)

    def test_answer_reveal_locked_for_first_two_attempts(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=False)
        self._take_quiz(correct=False)
        attempts = list(QuizAttempt.objects.filter(enrollment=self.enrollment, quiz=self.quiz)
                         .order_by('submitted_at'))
        self.assertFalse(attempts[0].reveal_answers)
        self.assertFalse(attempts[1].reveal_answers)

    def test_answer_reveal_unlocks_from_third_attempt(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=False)
        self._take_quiz(correct=False)
        self._take_quiz(correct=False)
        third = QuizAttempt.objects.filter(enrollment=self.enrollment, quiz=self.quiz).order_by('submitted_at')[2]
        self.assertTrue(third.reveal_answers)

    def test_best_score_is_the_max_across_attempts_not_the_latest(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=True)   # 100%
        self._take_quiz(correct=False)  # 0% -- a later, worse attempt

        best = self.enrollment.quiz_attempts.filter(quiz=self.quiz).order_by('-score_percent').first()
        self.assertEqual(best.score_percent, Decimal('100.00'))

    def test_quiz_result_view_scoped_to_own_attempt(self):
        self._complete_lecture()
        self.client.force_login(self.student)
        self._take_quiz(correct=True)
        attempt = QuizAttempt.objects.get(enrollment=self.enrollment, quiz=self.quiz)

        other_student = User.objects.create_user(username='quiz_stud2', password='pw', is_student=True)
        self.client.force_login(other_student)
        response = self.client.get(
            reverse('quiz_result', args=[self.course.id, self.module.id, attempt.id]))
        self.assertEqual(response.status_code, 404)

    def test_taking_quiz_requires_enrollment(self):
        stranger = User.objects.create_user(username='quiz_stranger', password='pw', is_student=True)
        self.client.force_login(stranger)
        response = self._take_quiz(correct=True)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class QuizCertificateEmailTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='email_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='email_stud', password='pw', is_student=True, email='learner@example.com')
        track = Track.objects.create(name='Email Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Email Course', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, price=Decimal('0.00'),
            is_free=True, status=Course.Status.PUBLISHED,
        )
        self.module = Module.objects.create(course=self.course, title='Module 1')
        self.lecture = Lecture.objects.create(module=self.module, title='Only Lecture')
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def test_completing_course_sends_certificate_email_with_pdf_attached(self):
        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['learner@example.com'])
        self.assertIn('Email Course', sent.subject)
        attachment_names = [a[0] for a in sent.attachments]
        self.assertEqual(len(attachment_names), 1)
        self.assertTrue(attachment_names[0].endswith('.pdf'))
        self.assertEqual(sent.attachments[0][2], 'application/pdf')

    def test_student_with_no_email_does_not_crash_completion(self):
        self.student.email = ''
        self.student.save()
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Certificate.objects.filter(enrollment=self.enrollment).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_email_send_failure_does_not_block_certificate_issuance(self):
        self.client.force_login(self.student)
        with patch('courses.emails.EmailMultiAlternatives.send', side_effect=Exception('smtp down')):
            response = self.client.post(
                reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Certificate.objects.filter(enrollment=self.enrollment).exists())

    def test_certificate_email_uses_updated_copy_and_links(self):
        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))

        sent = mail.outbox[0]
        certificate = Certificate.objects.get(enrollment=self.enrollment)
        self.assertEqual(sent.subject, "🎓 Congratulations! You've completed Email Course")
        self.assertIn('Well done,', sent.body)
        self.assertIn('related courses', sent.body)
        self.assertIn('linkedin.com/profile/add', sent.body)
        self.assertIn(f'verify/{certificate.uuid}', sent.body)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class AdminTestEmailToolTests(TestCase):
    """The admin-only 'send a real test email to yourself' utility --
    exists because Render's free tier has no Shell, so this is the only way
    to trigger a real send without deploying a one-off script."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='email_tool_admin', password='pw', email='admin@example.com',
            first_name='Ada', last_name='Admin')
        self.client.force_login(self.admin)

    def test_welcome_test_send_goes_to_typed_target(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'welcome', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wherever@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Welcome to Mendoura! 🎉')

    def test_welcome_test_requires_target_email(self):
        response = self.client.post(reverse('send_test_emails'), {'which': 'welcome', 'target_email': ''})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_enrollment_confirmation_test_send_uses_real_enrollment_and_typed_target(self):
        student = User.objects.create_user(username='tool_enroll_stud', password='pw', is_student=True)
        instructor = User.objects.create_user(username='tool_enroll_inst', password='pw', is_instructor=True)
        track = Track.objects.create(name='Tool Enroll Track')
        course = Course.objects.create(
            instructor=instructor, track=track, title='Tool Enroll Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True)
        Enrollment.objects.create(student=student, course=course)
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'enrollment_confirmation', 'target_email': 'wherever9@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever9@example.com'])
        self.assertIn('Tool Enroll Course', sent.subject)

    def test_enrollment_confirmation_test_send_without_any_enrollment_shows_error_not_crash(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'enrollment_confirmation', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_welcome_test_send_shows_real_error_instead_of_false_success(self):
        # Regression test for the actual bug reported: "sent successfully"
        # shown in the UI even though the message never left the server.
        # send_mail()/EmailMessage.send() raising (auth failure, refused
        # connection, etc.) must surface as an error message with the real
        # reason, not a success toast.
        with patch('django.core.mail.message.EmailMessage.send',
                   side_effect=Exception('535 Authentication Failed')):
            response = self.client.post(
                reverse('send_test_emails'),
                {'which': 'welcome', 'target_email': 'wherever@example.com'}, follow=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Send failed')
        self.assertContains(response, '535 Authentication Failed')
        messages_list = list(response.context['messages'])
        self.assertTrue(all(m.level_tag != 'success' for m in messages_list))

    def test_welcome_test_send_zero_delivered_without_exception_is_not_success(self):
        # fail_silently=True would swallow a failure into "0 delivered, no
        # exception" -- guard the other half of that same failure mode:
        # even without an exception, 0 delivered must not read as success.
        with patch('django.core.mail.message.EmailMessage.send', return_value=0):
            response = self.client.post(
                reverse('send_test_emails'),
                {'which': 'welcome', 'target_email': 'wherever@example.com'}, follow=True)
        self.assertContains(response, 'Send failed')
        messages_list = list(response.context['messages'])
        self.assertTrue(all(m.level_tag != 'success' for m in messages_list))

    def test_instructor_rejection_test_send_goes_to_typed_target(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'instructor_rejection', 'target_email': 'wherever3@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wherever3@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'An update on your Mendoura instructor application')

    def test_course_approved_test_send_uses_real_course_and_typed_target(self):
        instructor = User.objects.create_user(username='tool_course_inst', password='pw', is_instructor=True)
        track = Track.objects.create(name='Tool Course Track')
        course = Course.objects.create(
            instructor=instructor, track=track, title='Tool Test Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True)
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'course_approved', 'target_email': 'wherever4@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever4@example.com'])
        self.assertIn(course.title, sent.subject)

    def test_course_rejected_test_send_uses_real_course_and_typed_target(self):
        instructor = User.objects.create_user(username='tool_course_inst2', password='pw', is_instructor=True)
        track = Track.objects.create(name='Tool Course Track 2')
        Course.objects.create(
            instructor=instructor, track=track, title='Tool Test Course 2', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            rejection_reason='Needs better audio quality')
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'course_rejected', 'target_email': 'wherever5@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever5@example.com'])
        self.assertIn('Needs better audio quality', sent.body)

    def test_course_approved_test_send_without_any_course_shows_error_not_crash(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'course_approved', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_track_request_notification_test_send_uses_real_request_and_typed_target(self):
        instructor = User.objects.create_user(username='tool_track_inst', password='pw', is_instructor=True)
        category = Track.objects.create(name='Tool Track Category')
        TrackRequest.objects.create(instructor=instructor, parent=category, name='Tool Test Track')
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_notification', 'target_email': 'wherever6@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever6@example.com'])
        self.assertIn('Tool Test Track', sent.subject)

    def test_track_request_notification_test_send_without_any_request_shows_error_not_crash(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_notification', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_track_request_approved_test_send_uses_real_request_and_typed_target(self):
        instructor = User.objects.create_user(
            username='tool_track_inst2', password='pw', is_instructor=True, email='ignored@example.com')
        category = Track.objects.create(name='Tool Track Category 2')
        track = Track.objects.create(parent=category, name='Tool Approved Track')
        TrackRequest.objects.create(
            instructor=instructor, parent=category, name='Tool Approved Track',
            status=TrackRequest.Status.APPROVED, track=track)
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_approved', 'target_email': 'wherever7@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever7@example.com'])
        self.assertIn('Tool Approved Track', sent.subject)

    def test_track_request_approved_test_send_without_any_approved_request_shows_error_not_crash(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_approved', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_track_request_rejected_test_send_uses_real_request_and_typed_target(self):
        instructor = User.objects.create_user(
            username='tool_track_inst3', password='pw', is_instructor=True, email='ignored2@example.com')
        category = Track.objects.create(name='Tool Track Category 3')
        TrackRequest.objects.create(
            instructor=instructor, parent=category, name='Tool Rejected Track',
            status=TrackRequest.Status.REJECTED, rejection_reason='Overlaps an existing track')
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_rejected', 'target_email': 'wherever8@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever8@example.com'])
        self.assertIn('Overlaps an existing track', sent.body)

    def test_track_request_rejected_test_send_without_any_rejected_request_shows_error_not_crash(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'track_request_rejected', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_certificate_test_send_uses_real_certificate_and_typed_target(self):
        instructor = User.objects.create_user(username='tool_inst', password='pw', is_instructor=True)
        student = User.objects.create_user(
            username='tool_stud', password='pw', is_student=True, email='real@example.com')
        track = Track.objects.create(name='Tool Track')
        course = Course.objects.create(
            instructor=instructor, track=track, title='Tool Course', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, is_free=True,
            status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=course, title='M1')
        lecture = Lecture.objects.create(module=module, title='L1')
        enrollment = Enrollment.objects.create(student=student, course=course)
        self.client.force_login(student)
        self.client.post(reverse('mark_lecture_complete', args=[course.id, lecture.id]))
        mail.outbox.clear()

        self.client.force_login(self.admin)
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'certificate', 'target_email': 'wherever2@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['wherever2@example.com'])
        self.assertIn('Tool Course', sent.subject)
        # Real student's email is never leaked as a recipient of the test send.
        self.assertNotIn('real@example.com', sent.to)

    def test_certificate_test_send_without_any_certificate_shows_error_not_crash(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'certificate', 'target_email': 'wherever@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_student_test_always_goes_to_admins_own_email(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'password_reset_student', 'target_email': 'someone-else@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['admin@example.com'])
        self.assertNotIn('someone-else@example.com', mail.outbox[0].to)
        self.assertEqual(mail.outbox[0].subject, 'Reset your Mendoura password')

    def test_password_reset_test_send_shows_real_error_instead_of_false_success(self):
        # Same regression as the welcome-email case above, for the
        # specific email type the bug report named (password reset).
        with patch('django.core.mail.message.EmailMessage.send',
                   side_effect=Exception('535 Authentication Failed')):
            response = self.client.post(
                reverse('send_test_emails'),
                {'which': 'password_reset_student', 'target_email': 'admin@example.com'}, follow=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Send failed')
        self.assertContains(response, '535 Authentication Failed')
        messages_list = list(response.context['messages'])
        self.assertTrue(all(m.level_tag != 'success' for m in messages_list))

    def test_password_reset_instructor_test_previews_instructor_copy_regardless_of_admin_role(self):
        # self.admin has is_instructor=False -- the preview override must
        # still render the Instructor template when explicitly asked for.
        self.assertFalse(self.admin.is_instructor)
        response = self.client.post(reverse('send_test_emails'), {'which': 'password_reset_instructor'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['admin@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Reset your Mendoura instructor password')
        self.assertIn('instructor account', mail.outbox[0].body)

    def test_instructor_welcome_test_send_goes_to_typed_target(self):
        response = self.client.post(reverse('send_test_emails'),
                                     {'which': 'instructor_welcome', 'target_email': 'wherever3@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wherever3@example.com'])
        self.assertEqual(mail.outbox[0].subject, "Welcome to Mendoura — Let's build your first course 🎓")

    def test_instructor_welcome_test_requires_target_email(self):
        response = self.client.post(reverse('send_test_emails'), {'which': 'instructor_welcome', 'target_email': ''})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_instructor_application_received_test_send_goes_to_typed_target(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'instructor_application_received', 'target_email': 'wherever4@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wherever4@example.com'])
        self.assertEqual(mail.outbox[0].subject, "We've received your Mendoura instructor application")

    def test_instructor_application_received_test_requires_target_email(self):
        response = self.client.post(
            reverse('send_test_emails'), {'which': 'instructor_application_received', 'target_email': ''})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)

    def test_instructor_application_notification_test_send_goes_to_typed_target(self):
        response = self.client.post(
            reverse('send_test_emails'),
            {'which': 'instructor_application_notification', 'target_email': 'wherever5@example.com'})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['wherever5@example.com'])
        self.assertIn('email_tool_admin', mail.outbox[0].body)
        self.assertIn(reverse('admin_users'), mail.outbox[0].body)

    def test_instructor_application_notification_test_requires_target_email(self):
        response = self.client.post(
            reverse('send_test_emails'), {'which': 'instructor_application_notification', 'target_email': ''})
        self.assertRedirects(response, reverse('send_test_emails'))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CertificateLinkedInShareTests(TestCase):
    def setUp(self):
        self.instructor = User.objects.create_user(
            username='li_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='li_stud', password='pw', is_student=True)
        track = Track.objects.create(name='LinkedIn Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='LinkedIn Course', description='...',
            production_type=Course.ProductionType.SCRIPT_ONLY, price=Decimal('0.00'),
            is_free=True, status=Course.Status.PUBLISHED,
        )
        module = Module.objects.create(course=self.course, title='Module 1')
        self.lecture = Lecture.objects.create(module=module, title='Only Lecture')
        self.enrollment = Enrollment.objects.create(student=self.student, course=self.course)

    def _complete_course(self):
        self.client.force_login(self.student)
        self.client.post(reverse('mark_lecture_complete', args=[self.course.id, self.lecture.id]))
        self.client.logout()
        return Certificate.objects.get(enrollment=self.enrollment)

    def test_certificate_page_has_linkedin_share_button_and_copy_link(self):
        certificate = self._complete_course()
        response = self.client.get(reverse('certificate_verify', args=[certificate.uuid]))
        self.assertContains(response, 'linkedin.com/profile/add')
        self.assertContains(response, 'startTask=CERTIFICATION_NAME')
        self.assertContains(response, 'LinkedIn Course')
        self.assertContains(response, 'copy-cert-link')

    def test_short_verify_url_alias_works_and_matches_long_form(self):
        # Both routes render the same view/template with the same context;
        # compare on content rather than raw bytes since each response embeds
        # its own freshly-generated CSRF token in the language-switch form.
        certificate = self._complete_course()
        short_response = self.client.get(f'/verify/{certificate.uuid}/')
        long_response = self.client.get(reverse('certificate_verify', args=[certificate.uuid]))
        self.assertEqual(short_response.status_code, 200)
        self.assertContains(short_response, 'LinkedIn Course')
        self.assertContains(long_response, 'LinkedIn Course')
        self.assertEqual(
            short_response.context['certificate'], long_response.context['certificate'])

    def test_linkedin_cert_url_points_at_short_verify_link(self):
        from urllib.parse import unquote
        from . import certificates
        certificate = self._complete_course()
        share_url = certificates.linkedin_share_url(certificate)
        self.assertIn(f'/verify/{certificate.uuid}/', unquote(share_url))


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CourseCreationWizardTests(TestCase):
    """The guided 4-step course creation wizard: Details -> Modules ->
    per-module Video/Script + Quiz -> Review & Submit. Replaces the old
    "create course, then figure out where to go next" flow."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='wizard_inst', password='pw', is_instructor=True, email='wizard_inst@example.com')
        self.other_instructor = User.objects.create_user(
            username='wizard_intruder', password='pw', is_instructor=True)
        self.parent_track = Track.objects.create(name='Wizard Parent')
        self.track = Track.objects.create(name='Wizard Leaf', parent=self.parent_track)

    def _course_details_payload(self, **overrides):
        data = {
            'title': 'Wizard Course', 'description': 'desc', 'track': self.track.id,
            'language': 'English', 'level': 'beginner', 'production_type': 'full',
            'price': '0', 'is_free': 'on',
        }
        data.update(overrides)
        return data

    def test_step1_creates_draft_course_and_redirects_to_step2(self):
        self.client.force_login(self.instructor)
        response = self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.assertEqual(course.status, Course.Status.DRAFT)
        self.assertEqual(course.instructor, self.instructor)
        self.assertRedirects(response, reverse('course_wizard_modules', args=[course.id]))

    def test_step2_requires_at_least_one_module_before_next_is_offered(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        response = self.client.get(reverse('course_wizard_modules', args=[course.id]))
        self.assertContains(response, 'Add a module to continue')
        self.assertNotContains(response, 'Next: Add Video')

        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        response = self.client.get(reverse('course_wizard_modules', args=[course.id]))
        self.assertContains(response, 'Next: Add Video')

    def test_step3_blocks_advance_without_video_for_full_production(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload(production_type='full'))
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)

        response = self.client.post(
            reverse('course_wizard_module_content', args=[course.id, module.id]),
            {'action': 'advance'}, follow=True)
        self.assertContains(response, 'Upload a video')
        lecture = Lecture.objects.get(module=module)
        self.assertFalse(lecture.bunny_video_id)
        self.assertFalse(lecture.video_url)

    def test_step3_external_video_url_satisfies_full_production_requirement(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload(production_type='full'))
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)

        self.client.post(reverse('course_wizard_module_content', args=[course.id, module.id]),
                          {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})
        response = self.client.post(
            reverse('course_wizard_module_content', args=[course.id, module.id]),
            {'action': 'advance'})
        self.assertRedirects(response, reverse('course_wizard_review', args=[course.id]))

    def test_step3_script_only_requires_script_not_video(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload(production_type='script_only'))
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)

        blocked = self.client.post(
            reverse('course_wizard_module_content', args=[course.id, module.id]),
            {'action': 'advance'}, follow=True)
        self.assertContains(blocked, 'Add your script')

        self.client.post(reverse('course_wizard_module_content', args=[course.id, module.id]),
                          {'action': 'save_script', 'script': 'Once upon a time...'})
        response = self.client.post(
            reverse('course_wizard_module_content', args=[course.id, module.id]),
            {'action': 'advance'})
        self.assertRedirects(response, reverse('course_wizard_review', args=[course.id]))
        lecture = Lecture.objects.get(module=module)
        self.assertEqual(lecture.ai_generated_script, 'Once upon a time...')

    def test_step3_quiz_question_and_choices_end_to_end(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)
        url = reverse('course_wizard_module_content', args=[course.id, module.id])

        self.client.post(url, {'action': 'save_quiz_settings', 'title': '', 'passing_score_percent': 70})
        quiz = Quiz.objects.get(module=module)
        self.client.post(url, {'action': 'add_question', 'text': 'What is 2+2?', 'order': 1})
        question = Question.objects.get(quiz=quiz)
        self.client.post(url, {'action': 'add_choice', 'question_id': question.id, 'text': '4', 'is_correct': 'on'})
        self.client.post(url, {'action': 'add_choice', 'question_id': question.id, 'text': '5'})

        choices = list(question.choices.all())
        self.assertEqual(len(choices), 2)
        self.assertEqual(sum(1 for c in choices if c.is_correct), 1)

    def test_skip_quiz_link_only_shown_before_a_quiz_exists(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)
        url = reverse('course_wizard_module_content', args=[course.id, module.id])

        response = self.client.get(url)
        self.assertContains(response, 'Skip Quiz')

        self.client.post(url, {'action': 'save_quiz_settings', 'title': '', 'passing_score_percent': 70})
        response = self.client.get(url)
        self.assertNotContains(response, 'Skip Quiz')

    def test_advancing_past_last_module_goes_to_review(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Only Module', 'order': 1})
        module = Module.objects.get(course=course)
        url = reverse('course_wizard_module_content', args=[course.id, module.id])
        self.client.post(url, {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})

        response = self.client.post(url, {'action': 'advance'})
        self.assertRedirects(response, reverse('course_wizard_review', args=[course.id]))

    def test_review_submit_moves_course_to_pending_review(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Only Module', 'order': 1})
        module = Module.objects.get(course=course)
        self.client.post(reverse('course_wizard_module_content', args=[course.id, module.id]),
                          {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})

        response = self.client.post(reverse('course_wizard_review', args=[course.id]))
        self.assertRedirects(response, reverse('instructor_dashboard'))
        course.refresh_from_db()
        self.assertEqual(course.status, Course.Status.PENDING_REVIEW)

    def test_review_submit_sends_admin_notification(self):
        # Same pattern as the instructor-application notification: a
        # submitted course shouldn't sit unnoticed in the queue until an
        # admin happens to check.
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Only Module', 'order': 1})
        module = Module.objects.get(course=course)
        self.client.post(reverse('course_wizard_module_content', args=[course.id, module.id]),
                          {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})

        self.client.post(reverse('course_wizard_review', args=[course.id]))
        notification = next(
            m for m in mail.outbox
            if m.to == [settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL])
        self.assertIn('Wizard Course', notification.subject)
        self.assertIn('Wizard Course', notification.body)
        self.assertIn('wizard_inst', notification.body)
        self.assertIn(reverse('course_approval_queue'), notification.body)

    def test_review_blocks_submit_when_a_module_is_missing_content(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Only Module', 'order': 1})
        response = self.client.get(reverse('course_wizard_review', args=[course.id]))
        self.assertContains(response, 'Every module needs its video or script')
        self.assertNotContains(response, 'Submit for Review')

    def test_resume_lands_on_modules_step_when_no_modules_yet(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        response = self.client.get(reverse('course_wizard_resume', args=[course.id]))
        self.assertRedirects(response, reverse('course_wizard_modules', args=[course.id]))

    def test_resume_lands_on_first_incomplete_module(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 2', 'order': 2})
        module1 = Module.objects.get(title='Module 1')
        module2 = Module.objects.get(title='Module 2')
        self.client.post(reverse('course_wizard_module_content', args=[course.id, module1.id]),
                          {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})

        response = self.client.get(reverse('course_wizard_resume', args=[course.id]))
        self.assertRedirects(response, reverse('course_wizard_module_content', args=[course.id, module2.id]))

    def test_resume_lands_on_review_once_all_modules_ready(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)
        self.client.post(reverse('course_wizard_module_content', args=[course.id, module.id]),
                          {'action': 'save_video_url', 'video_url': 'https://youtube.com/watch?v=abc'})
        response = self.client.get(reverse('course_wizard_resume', args=[course.id]))
        self.assertRedirects(response, reverse('course_wizard_review', args=[course.id]))

    def test_wizard_unreachable_once_course_is_no_longer_draft(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        course.status = Course.Status.PENDING_REVIEW
        course.save()
        response = self.client.get(reverse('course_wizard_modules', args=[course.id]))
        self.assertRedirects(response, reverse('manage_modules', args=[course.id]))

    def test_other_instructor_cannot_access_someone_elses_wizard(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')

        self.client.force_login(self.other_instructor)
        response = self.client.get(reverse('course_wizard_modules', args=[course.id]))
        self.assertEqual(response.status_code, 404)

    def test_deleting_a_module_in_step2_removes_it(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'add', 'title': 'Module 1', 'order': 1})
        module = Module.objects.get(course=course)
        self.client.post(reverse('course_wizard_modules', args=[course.id]),
                          {'action': 'delete', 'module_id': module.id})
        self.assertFalse(Module.objects.filter(id=module.id).exists())

    def test_instructor_dashboard_shows_continue_setup_for_draft(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('create_course'), self._course_details_payload())
        course = Course.objects.get(title='Wizard Course')
        response = self.client.get(reverse('instructor_dashboard'))
        self.assertContains(response, reverse('course_wizard_resume', args=[course.id]))
        self.assertContains(response, 'Continue Setup')


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class QuizBuilderInstructorTests(TestCase):
    """Instructor-facing quiz builder: create/edit/delete quiz, questions,
    choices, and the "exactly one correct choice" enforcement."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='builder_inst', password='pw', is_instructor=True)
        self.intruder = User.objects.create_user(
            username='builder_intruder', password='pw', is_instructor=True)
        track = Track.objects.create(name='Builder Track')
        self.course = Course.objects.create(
            instructor=self.instructor, track=track, title='Builder Course', description='...',
            production_type=Course.ProductionType.FULL, price=Decimal('0.00'), is_free=True,
            status=Course.Status.PUBLISHED,
        )
        self.module = Module.objects.create(course=self.course, title='M1')

    def test_creating_quiz_settings_creates_quiz(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('manage_quiz', args=[self.course.id, self.module.id]), {
            'title': '', 'passing_score_percent': 80,
        })
        quiz = Quiz.objects.get(module=self.module)
        self.assertEqual(quiz.passing_score_percent, 80)
        self.assertEqual(quiz.display_title, 'M1 Quiz')

    def test_editing_published_course_quiz_reenters_review(self):
        self.client.force_login(self.instructor)
        self.client.post(reverse('manage_quiz', args=[self.course.id, self.module.id]), {
            'title': '', 'passing_score_percent': 70,
        })
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.PENDING_REVIEW)

    def test_add_question_and_choices(self):
        self.client.force_login(self.instructor)
        quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.client.post(reverse('add_question', args=[self.course.id, self.module.id]),
                          {'text': 'What is 2+2?', 'order': 1})
        question = Question.objects.get(quiz=quiz)

        self.client.post(reverse('add_choice', args=[question.id]), {'text': '4', 'is_correct': 'on'})
        self.client.post(reverse('add_choice', args=[question.id]), {'text': '5'})
        choices = list(question.choices.all())
        self.assertEqual(len(choices), 2)
        self.assertEqual(sum(1 for c in choices if c.is_correct), 1)

    def test_add_question_redirects_straight_to_choices_editor(self):
        # Regression test: a freshly added question has no answer choices
        # yet, so the next step is never optional -- land the instructor
        # directly on the page where they add them, instead of back on the
        # quiz overview where the only way to discover that step exists is
        # an easy-to-miss "Edit / Choices" link.
        self.client.force_login(self.instructor)
        Quiz.objects.create(module=self.module, passing_score_percent=70)
        response = self.client.post(reverse('add_question', args=[self.course.id, self.module.id]),
                                     {'text': 'What is 2+2?', 'order': 1})
        question = Question.objects.get(text='What is 2+2?')
        self.assertRedirects(response, reverse('edit_question', args=[question.id]))

    def test_marking_a_new_choice_correct_unmarks_the_others(self):
        self.client.force_login(self.instructor)
        quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        question = Question.objects.create(quiz=quiz, text='Q', order=1)
        c1 = Choice.objects.create(question=question, text='A', is_correct=True)
        c2 = Choice.objects.create(question=question, text='B', is_correct=False)

        self.client.post(reverse('mark_choice_correct', args=[c2.id]))

        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertFalse(c1.is_correct)
        self.assertTrue(c2.is_correct)

    def test_deleting_question_cascades_choices(self):
        self.client.force_login(self.instructor)
        quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        question = Question.objects.create(quiz=quiz, text='Q', order=1)
        Choice.objects.create(question=question, text='A', is_correct=True)

        self.client.post(reverse('delete_question', args=[question.id]))
        self.assertFalse(Question.objects.filter(id=question.id).exists())
        self.assertFalse(Choice.objects.filter(question_id=question.id).exists())

    def test_deleting_quiz_removes_it_entirely(self):
        self.client.force_login(self.instructor)
        Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.client.post(reverse('delete_quiz', args=[self.course.id, self.module.id]))
        self.assertFalse(Quiz.objects.filter(module=self.module).exists())

    def test_other_instructor_cannot_manage_quiz(self):
        Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.client.force_login(self.intruder)
        response = self.client.get(reverse('manage_quiz', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 404)

    def test_other_instructor_cannot_add_question(self):
        Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.client.force_login(self.intruder)
        response = self.client.post(
            reverse('add_question', args=[self.course.id, self.module.id]), {'text': 'hijack', 'order': 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Question.objects.count(), 0)

    def test_other_instructor_cannot_add_choice_or_mark_correct(self):
        quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        question = Question.objects.create(quiz=quiz, text='Q', order=1)
        choice = Choice.objects.create(question=question, text='A', is_correct=False)

        self.client.force_login(self.intruder)
        response = self.client.post(reverse('add_choice', args=[question.id]), {'text': 'hijack'})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse('mark_choice_correct', args=[choice.id]))
        self.assertEqual(response.status_code, 404)
        choice.refresh_from_db()
        self.assertFalse(choice.is_correct)

    def test_other_instructor_cannot_delete_question_or_choice(self):
        quiz = Quiz.objects.create(module=self.module, passing_score_percent=70)
        question = Question.objects.create(quiz=quiz, text='Q', order=1)
        choice = Choice.objects.create(question=question, text='A', is_correct=False)

        self.client.force_login(self.intruder)
        self.assertEqual(
            self.client.post(reverse('delete_question', args=[question.id])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse('delete_choice', args=[choice.id])).status_code, 404)
        self.assertTrue(Question.objects.filter(id=question.id).exists())
        self.assertTrue(Choice.objects.filter(id=choice.id).exists())

    def test_other_instructor_cannot_delete_quiz(self):
        Quiz.objects.create(module=self.module, passing_score_percent=70)
        self.client.force_login(self.intruder)
        response = self.client.post(reverse('delete_quiz', args=[self.course.id, self.module.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Quiz.objects.filter(module=self.module).exists())
