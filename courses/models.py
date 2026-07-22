import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import get_language

from . import ai_translate, certificates
from .money import SUBSCRIPTION_INSTRUCTOR_SHARE, calculate_split, get_instructor_share


class User(AbstractUser):
    is_student = models.BooleanField(default=False)
    is_instructor = models.BooleanField(default=False)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    # Uses the default storage backend (Cloudinary -- see STORAGES in
    # settings.py), same as course thumbnails.
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    # Defaults to True so every existing account (admins, seeded users, any
    # User created outside the public signup forms) stays unaffected -- only
    # StudentSignUpForm/InstructorSignUpForm explicitly flip this to False,
    # putting new self-service signups into a pending-admin-review state.
    is_approved = models.BooleanField(default=True)

    def __str__(self):
        return self.username

    @property
    def initials(self):
        return (self.username[:1] or '?').upper()


def _unique_slugify(instance, base_value, slug_field='slug'):
    """Generate a unique slug for instance's model, retrying with -2, -3, ... on collision."""
    slug = slugify(base_value)
    ModelClass = instance.__class__
    candidate = slug
    n = 2
    while ModelClass.objects.filter(**{slug_field: candidate}).exclude(pk=instance.pk).exists():
        candidate = f'{slug}-{n}'
        n += 1
    return candidate


class AutoTranslatedFieldsMixin:
    """AI-populated per-language JSON translations for a model's designated
    text fields. The admin/instructor-entered value is always assumed to be
    written in SOURCE_LANGUAGE (English); on save(), any field whose source
    text has changed (or whose translations don't yet cover every active
    non-source language) gets a single batched translation call. Subclasses
    must define TRANSLATABLE_FIELDS and a `{field}_translations` JSONField
    for each entry.

    Never blocks a save: if AI_API_KEY isn't configured, or the translation
    call fails for any reason, a subclass-defined LOCAL_TRANSLATIONS
    dictionary (if any) fills in whatever it knows about, and anything
    still missing falls back to the English source via `_translated()`."""
    SOURCE_LANGUAGE = 'en'
    TRANSLATABLE_FIELDS = ()
    # Optional per-model static fallback, keyed by field then by exact
    # English source text: {"name": {"Cybersecurity": {"ar": "..."}}}.
    # Used whenever the AI API is unavailable or fails, so a handful of
    # well-known values (e.g. the seeded track catalog) show up translated
    # immediately instead of waiting on a real API key. AI results always
    # take priority over this when both are available.
    LOCAL_TRANSLATIONS = {}

    def _translated(self, field):
        translations = getattr(self, f'{field}_translations') or {}
        lang = get_language() or self.SOURCE_LANGUAGE
        return translations.get(lang) or getattr(self, field)

    def _autotranslate(self):
        target_languages = [code for code, _label in settings.LANGUAGES if code != self.SOURCE_LANGUAGE]
        if not target_languages:
            return

        pending = {}
        for field in self.TRANSLATABLE_FIELDS:
            source_value = (getattr(self, field, '') or '').strip()
            if not source_value:
                continue
            translations = getattr(self, f'{field}_translations') or {}
            stale = translations.get('__source__') != source_value
            incomplete = any(lang not in translations for lang in target_languages)
            if stale or incomplete:
                pending[field] = source_value

        if not pending:
            return

        results = {}
        if ai_translate.is_configured():
            try:
                results = ai_translate.translate_fields(pending, target_languages)
            except ai_translate.TranslationError:
                results = {}

        for field, source_value in pending.items():
            translated = dict(results.get(field) or {})
            local_fallback = self.LOCAL_TRANSLATIONS.get(field, {}).get(source_value, {})
            for lang, text in local_fallback.items():
                translated.setdefault(lang, text)
            if not translated:
                continue
            translated['__source__'] = source_value
            setattr(self, f'{field}_translations', translated)


class Track(AutoTranslatedFieldsMixin, models.Model):
    # Self-referencing rather than a separate "Category" model for the top level --
    # a track and its parent are the same kind of thing (name, slug, icon, ordering),
    # just nested. Two levels deep in practice (Tech -> Cybersecurity), but nothing
    # here enforces that depth limit.
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='children')
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, help_text=_('e.g. a Font Awesome class name'))
    cover_image = models.ImageField(upload_to='track_covers/', blank=True, null=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # Auto-populated by AutoTranslatedFieldsMixin -- {"ar": "...", "fr": "...", "es": "..."}.
    name_translations = models.JSONField(default=dict, blank=True)
    description_translations = models.JSONField(default=dict, blank=True)
    TRANSLATABLE_FIELDS = ('name', 'description')

    # Hand-written Arabic for the core seeded tracks (see seed_tracks.py) --
    # used whenever the AI API is missing or fails, so these show up
    # translated on the live site immediately rather than waiting on a key.
    # Only Arabic is provided; French/Spanish still fall back to English
    # until a real AI translation succeeds for those.
    LOCAL_TRANSLATIONS = {
        'name': {
            'Cybersecurity': {'ar': 'الأمن السيبراني'},
            'Artificial Intelligence & Machine Learning': {'ar': 'الذكاء الاصطناعي وتعلم الآلة'},
            'Mobile Development': {'ar': 'تطوير تطبيقات الهاتف'},
            'Web Development': {'ar': 'تطوير الويب'},
            'Tech': {'ar': 'تكنولوجيا'},
            'Business': {'ar': 'إدارة أعمال'},
            'Marketing': {'ar': 'تسويق'},
            'Design': {'ar': 'تصميم'},
            'Languages': {'ar': 'اللغات'},
        },
    }

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return f'{self.parent.name} / {self.name}' if self.parent else self.name

    @property
    def translated_name(self):
        return self._translated('name')

    @property
    def translated_description(self):
        return self._translated('description')

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slugify(self, self.name)
        self._autotranslate()
        super().save(*args, **kwargs)


class TrackRoadmapStep(models.Model):
    """One node in a track's ordered learning path (the track roadmap, distinct
    from a course's own Module/Lecture syllabus). `course` is nullable so an
    admin can lay out a full planned roadmap -- 'Python Fundamentals', 'Machine
    Learning Foundations', ... -- before any instructor has actually published
    the matching course. Once a real course exists, link it here and the
    stepper switches from a plain label to a clickable, progress-aware node."""
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name='roadmap_steps')
    course = models.ForeignKey('Course', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='roadmap_steps')
    title = models.CharField(max_length=255, help_text=_('Used until a course is linked, or if none ever is.'))
    order = models.PositiveIntegerField(default=0)
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.track.name} #{self.order}: {self.display_title}'

    @property
    def display_title(self):
        return self.course.title if self.course else self.title


class Course(models.Model):
    class Level(models.TextChoices):
        BEGINNER = 'beginner', _('Beginner')
        INTERMEDIATE = 'intermediate', _('Intermediate')
        ADVANCED = 'advanced', _('Advanced')

    class ProductionType(models.TextChoices):
        FULL = 'full', _('Full production by instructor')
        SCRIPT_ONLY = 'script_only', _('Script only (platform produces)')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        PENDING_REVIEW = 'pending_review', _('Pending Review')
        PUBLISHED = 'published', _('Published')
        REJECTED = 'rejected', _('Rejected')
        # "Deleted" from the instructor's point of view, but the row stays --
        # on_delete=PROTECT on Payment/RevenueDistribution/WatchEvent and the
        # explicit Enrollment check in delete_course() mean a course with any
        # money or watch-time history can never be hard-deleted.
        ARCHIVED = 'archived', _('Archived')

    instructor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='courses',
        limit_choices_to={'is_instructor': True}
    )
    # null=True: no Track exists to default to; required at the form layer instead.
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name='courses', null=True)

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True, default='')
    subtitle = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField()
    what_you_will_learn = models.TextField(blank=True, default='')
    requirements = models.TextField(blank=True, default='')
    language = models.CharField(max_length=50, default='English')
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.BEGINNER)
    duration_hours = models.PositiveIntegerField(default=0)

    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    is_free = models.BooleanField(default=False)

    # null=True: never silently default a field that drives revenue split.
    production_type = models.CharField(max_length=20, choices=ProductionType.choices, null=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    rejection_reason = models.TextField(blank=True, default='')

    thumbnail = models.ImageField(upload_to='course_thumbnails/', blank=True, null=True)
    ai_script = models.TextField(help_text=_('The script for AI video generation'), blank=True, null=True)

    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.00'))
    students_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    def has_successful_sale(self):
        return self.payments.filter(status=Payment.Status.SUCCEEDED).exists()

    def get_instructor_share_percentage(self) -> Decimal:
        return get_instructor_share(self.production_type)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slugify(self, self.title)
        if self.pk and self.production_type:
            previous = Course.objects.only('production_type').get(pk=self.pk)
            if previous.production_type != self.production_type and self.has_successful_sale():
                raise ValidationError(
                    _("production_type is read-only once a course has its first successful sale.")
                )
        super().save(*args, **kwargs)


class Module(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='modules')
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.course.title} - {self.title}'


class Lecture(models.Model):
    class ContentType(models.TextChoices):
        VIDEO = 'video', _('Video')
        ARTICLE = 'article', _('Article')
        QUIZ = 'quiz', _('Quiz')

    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='lectures', null=True)
    title = models.CharField(max_length=255)
    content_type = models.CharField(max_length=20, choices=ContentType.choices, default=ContentType.VIDEO)
    # Bunny Stream video GUID. When set, the player embeds Bunny's token-signed
    # iframe instead of the legacy video_url/video_file. Those two stay for
    # backward compatibility with any lecture created before the Bunny switch.
    bunny_video_id = models.CharField(max_length=64, blank=True, default='')
    # Bunny encoding status, updated by its webhook: 0 created, 1 uploaded,
    # 2 processing, 3 transcoding, 4 finished, 5 error, 6 upload-failed.
    # The embed player copes with any state on its own; this just lets the
    # instructor see when a freshly uploaded video is actually ready.
    bunny_status = models.PositiveSmallIntegerField(default=0)
    video_url = models.URLField(blank=True, null=True)
    video_file = models.FileField(upload_to='lecture_videos/', blank=True, null=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    is_preview = models.BooleanField(default=False, help_text=_('Viewable for free without enrollment'))
    accepts_submission = models.BooleanField(
        default=False, help_text=_('Students can upload homework for this lecture'))
    order = models.PositiveIntegerField(default=0)
    ai_generated_script = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.module.course.title} - {self.title}'

    @property
    def course(self):
        return self.module.course

    @property
    def bunny_ready(self):
        # 4 == "finished" in Bunny's status enum. Anything below means the
        # video is still being uploaded/transcoded and isn't watchable yet.
        return bool(self.bunny_video_id) and self.bunny_status >= 4


class Resource(models.Model):
    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name='resources')
    file = models.FileField(upload_to='lecture_resources/',
                             help_text=_('Any file: PDF, zip, image, audio, slides, etc.'))
    title = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0, help_text=_('Size in bytes'))

    def __str__(self):
        return self.title or self.file.name


class Submission(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='submissions')
    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name='submissions')
    submitted_file = models.FileField(upload_to='submissions/', blank=True, null=True)
    submission_link = models.URLField(blank=True, null=True, help_text=_('Link to Google Drive, GitHub, etc.'))
    note = models.TextField(blank=True, null=True)
    grade = models.CharField(max_length=50, blank=True, null=True)
    feedback = models.TextField(blank=True, null=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set the moment an instructor grades this submission. Once set, the
    # student can no longer edit it -- mirrors how Payment/WalletTransaction
    # freeze fields once a record has been acted on downstream.
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-submitted_at']
        unique_together = ('student', 'lecture')

    def __str__(self):
        return f"{self.student.username} - {self.lecture.title}"

    @property
    def is_graded(self):
        return self.graded_at is not None


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        SUCCEEDED = 'succeeded', _('Succeeded')
        FAILED = 'failed', _('Failed')
        REFUNDED = 'refunded', _('Refunded')

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                 related_name='payments')
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name='payments')

    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='USD')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # ═══ Frozen at creation. Immutable forever. ═══
    production_type_at_purchase = models.CharField(max_length=20, editable=False)
    instructor_share_percentage = models.DecimalField(max_digits=5, decimal_places=2, editable=False)
    instructor_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    platform_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

    provider_transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True,
                                                db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # The DB itself rejects a bad split, even if the code is wrong.
            models.CheckConstraint(
                condition=models.Q(total_amount=models.F('instructor_amount') + models.F('platform_amount')),
                name='payment_split_sums_to_total',
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0),
                name='payment_amount_non_negative',
            ),
        ]

    def __str__(self):
        return f'{self.student} -> {self.course} (${self.total_amount})'

    def save(self, *args, **kwargs):
        if self._state.adding:
            # Percentage is READ FROM THE COURSE -- never passed in by hand.
            self.production_type_at_purchase = self.course.production_type
            self.instructor_share_percentage = get_instructor_share(self.production_type_at_purchase)
            self.instructor_amount, self.platform_amount = calculate_split(
                self.total_amount, self.instructor_share_percentage)
        else:
            # Frozen fields cannot change after creation.
            frozen = Payment.objects.only(
                'total_amount', 'instructor_amount', 'platform_amount',
                'instructor_share_percentage').get(pk=self.pk)
            for f in ('total_amount', 'instructor_amount',
                      'platform_amount', 'instructor_share_percentage'):
                if getattr(self, f) != getattr(frozen, f):
                    raise ValidationError(_("'%(field)s' is immutable after creation.") % {'field': f})
        super().save(*args, **kwargs)


class Enrollment(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='enrollments')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollments')
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='enrollment')
    # True when access came from an active Subscription rather than a free
    # course or a one-off Payment. Informational only -- access itself is
    # always re-checked live via student_has_access(), never trusted from
    # this flag alone, so an expired subscription can't leave stale access.
    via_subscription = models.BooleanField(default=False)
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('student', 'course')
        ordering = ['-enrolled_at']

    def __str__(self):
        return f'{self.student} enrolled in {self.course}'

    def total_lecture_count(self):
        return Lecture.objects.filter(module__course=self.course).count()

    def completed_lecture_count(self):
        return self.lecture_progress.filter(completed=True).count()

    def progress_percent(self):
        total = self.total_lecture_count()
        if not total:
            return 0
        return round(self.completed_lecture_count() * 100 / total)

    def is_complete(self):
        total = self.total_lecture_count()
        return total > 0 and self.completed_lecture_count() >= total

    def issue_certificate_if_complete(self):
        if self.is_complete():
            certificate, created = Certificate.objects.get_or_create(enrollment=self)
            if created:
                certificate.generate_pdf()
            return certificate
        return None


class LectureProgress(models.Model):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name='lecture_progress')
    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name='progress_entries')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_position_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('enrollment', 'lecture')

    def __str__(self):
        return f'{self.enrollment.student} - {self.lecture} ({"done" if self.completed else "in progress"})'


class InstructorWallet(models.Model):
    instructor = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        limit_choices_to={'is_instructor': True}
    )
    total_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    available_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    pending_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_withdrawn = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.instructor.username}'s Wallet"


class WalletTransaction(models.Model):
    class Type(models.TextChoices):
        SALE_CREDIT = 'sale_credit', _('Sale Credit')
        WITHDRAWAL = 'withdrawal', _('Withdrawal')
        ADJUSTMENT = 'adjustment', _('Adjustment')
        REFUND_DEBIT = 'refund_debit', _('Refund Debit')

    wallet = models.ForeignKey(InstructorWallet, on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='wallet_transactions')
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.wallet.instructor.username}: {self.get_type_display()} ${self.amount}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(_('WalletTransaction rows are append-only and cannot be modified.'))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_('WalletTransaction rows are append-only and cannot be deleted.'))


class Payout(models.Model):
    class Status(models.TextChoices):
        REQUESTED = 'requested', _('Requested')
        APPROVED = 'approved', _('Approved')
        PAID = 'paid', _('Paid')
        REJECTED = 'rejected', _('Rejected')

    wallet = models.ForeignKey(InstructorWallet, on_delete=models.CASCADE, related_name='payouts')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    method = models.CharField(max_length=100, blank=True)
    admin_note = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-requested_at']

    def __str__(self):
        return f'{self.wallet.instructor.username} payout ${self.amount} ({self.status})'


class Plan(models.Model):
    """An all-access subscription tier. Model, not a settings constant, so
    pricing is admin-editable without a deploy."""
    class Interval(models.TextChoices):
        MONTHLY = 'monthly', _('Monthly')
        ANNUAL = 'annual', _('Annual')

    name = models.CharField(max_length=100)
    interval = models.CharField(max_length=20, choices=Interval.choices, default=Interval.ANNUAL)
    price_egp = models.DecimalField(max_digits=10, decimal_places=2)
    price_usd = models.DecimalField(max_digits=10, decimal_places=2)
    duration_days = models.PositiveIntegerField(default=365)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['duration_days']

    def __str__(self):
        return self.name


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', _('Active')
        EXPIRED = 'expired', _('Expired')
        CANCELED = 'canceled', _('Canceled')

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # Frozen at purchase, like Payment's frozen split fields -- a later price
    # change on Plan must not rewrite what this student actually paid.
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    currency = models.CharField(max_length=3, default='EGP', editable=False)

    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    provider_transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True,
                                                db_index=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.student} / {self.plan.name} ({self.status})'

    def is_active_now(self):
        return self.status == self.Status.ACTIVE and self.expires_at > timezone.now()


class WatchEvent(models.Model):
    """Append-only raw ledger of watch-time, flushed client-side every ~30s
    (never one row per heartbeat -- that's millions of rows a day). This is
    the only source of truth the revenue-distribution job trusts; nothing
    about "how much a student watched" is ever taken from the client at
    distribution time, only re-aggregated from these rows."""
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='watch_events')
    lecture = models.ForeignKey(Lecture, on_delete=models.PROTECT, related_name='watch_events')
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name='watch_events')
    seconds_watched = models.PositiveIntegerField()
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-occurred_at']

    def __str__(self):
        return f'{self.student} watched {self.seconds_watched}s of {self.lecture}'


class SubscriptionPeriod(models.Model):
    """One row per Subscription lifetime (not re-sliced monthly even for the
    annual plan -- distribution happens once the period ends). amount_paid is
    a snapshot: the subscription's own price can't retroactively change what
    was actually paid."""
    class Status(models.TextChoices):
        OPEN = 'open', _('Open')
        DISTRIBUTED = 'distributed', _('Distributed')
        CANCELED = 'canceled', _('Canceled')

    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name='periods')
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    currency = models.CharField(max_length=3, default='EGP', editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    distributed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-period_start']

    def __str__(self):
        return f'{self.subscription} period {self.period_start:%Y-%m-%d} - {self.period_end:%Y-%m-%d}'


class RevenueDistribution(models.Model):
    """Audit trail: one row per (period, course) the distribution job pays
    out. attributed_amount is this course's watch-time share of the whole
    period's amount_paid; instructor_amount/platform_amount is the flat
    subscription split (SUBSCRIPTION_INSTRUCTOR_SHARE) applied to that slice
    -- never the course's own production_type-based split, which only
    applies to direct one-off sales."""
    period = models.ForeignKey(SubscriptionPeriod, on_delete=models.PROTECT, related_name='distributions')
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name='revenue_distributions')
    instructor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name='revenue_distributions')

    seconds_watched = models.PositiveIntegerField()
    share_of_period = models.DecimalField(max_digits=7, decimal_places=6)
    attributed_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    instructor_share_pct = models.DecimalField(max_digits=5, decimal_places=2, editable=False)
    instructor_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    platform_amount = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

    wallet_transaction = models.ForeignKey(WalletTransaction, on_delete=models.PROTECT,
                                            related_name='revenue_distribution', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.period} / {self.course}: {self.instructor_amount} to {self.instructor}'

    @property
    def minutes_watched(self):
        return self.seconds_watched // 60

    @property
    def share_of_period_pct(self):
        return (self.share_of_period * 100).quantize(Decimal('0.1'))


class Review(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reviews')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('student', 'course')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.student.username} rated {self.course.title}: {self.rating}/5'


class Certificate(models.Model):
    enrollment = models.OneToOneField(Enrollment, on_delete=models.CASCADE, related_name='certificate')
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    issued_at = models.DateTimeField(auto_now_add=True)
    pdf_file = models.FileField(upload_to='certificates/', blank=True, null=True)

    def __str__(self):
        return f'Certificate for {self.enrollment}'

    def generate_pdf(self):
        """Renders the certificate PDF and saves it into pdf_file."""
        from django.core.files.base import ContentFile

        pdf_bytes = certificates.build_certificate_pdf(self)
        self.pdf_file.save(f'certificate-{self.uuid}.pdf', ContentFile(pdf_bytes), save=True)
        return self.pdf_file


class AIConversation(models.Model):
    """One AI Study Buddy chat thread. Students can have several over time;
    the dashboard view always resumes the most recent one."""
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ai_conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'AI chat with {self.student.username} ({self.pk})'


class AIMessage(models.Model):
    class Role(models.TextChoices):
        USER = 'user', _('User')
        ASSISTANT = 'assistant', _('Assistant')

    conversation = models.ForeignKey(AIConversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.role}: {self.content[:50]}'
