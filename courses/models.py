import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import get_language

from . import auto_translate, bunny, certificates, emails, poster
from .money import SUBSCRIPTION_INSTRUCTOR_SHARE, calculate_split, get_instructor_share

logger = logging.getLogger(__name__)


#: Values that count as "based in Egypt" for the instructor Payoneer gate --
#: matched case-insensitively against the free-text country field.
EGYPT_ALIASES = {'egypt', 'eg', 'arab republic of egypt', 'مصر', 'جمهورية مصر العربية'}


class SpacesAllowedUsernameValidator(RegexValidator):
    """Same as Django's default UnicodeUsernameValidator, minus the ban on
    spaces -- usernames here are never used in URLs, @mentions, or as any
    kind of identifier outside the DB, so there's no technical reason to
    reject them."""
    regex = r'^[\w.@+ -]+\Z'
    message = _(
        'Enter a valid username. This value may contain only letters, '
        'numbers, spaces, and @/./+/-/_ characters.'
    )
    flags = 0


class User(AbstractUser):
    # Overrides AbstractUser's default username field only to swap in a
    # validator that allows spaces (see SpacesAllowedUsernameValidator) --
    # everything else (max_length, uniqueness, error message) matches
    # Django's default exactly.
    username_validator = SpacesAllowedUsernameValidator()
    username = models.CharField(
        _('username'),
        max_length=150,
        unique=True,
        help_text=_('Required. 150 characters or fewer. Letters, digits, spaces and @/./+/-/_ only.'),
        validators=[username_validator],
        error_messages={
            'unique': _('A user with that username already exists.'),
        },
    )

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

    country = models.CharField(max_length=100, blank=True, default='')
    # Required at signup for instructors based outside Egypt (see
    # InstructorSignUpForm.clean) -- their payout destination.
    payoneer_account = models.CharField(
        max_length=255, blank=True, default='',
        help_text=_('Payoneer email or account ID. Required for instructors based outside Egypt.'))

    # Timestamped consent, set once at signup and never overwritten --
    # proof of what a user agreed to and when, not just that they currently
    # have an "agree" checkbox ticked somewhere. Covers the Terms &
    # Conditions as a whole, including the revenue-share (Section 5) and
    # tax-responsibility (Section 6) clauses -- those aren't acknowledged
    # separately.
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.username

    @property
    def initials(self):
        return (self.username[:1] or '?').upper()

    @property
    def is_international_instructor(self):
        return self.is_instructor and self.country.strip().lower() not in EGYPT_ALIASES


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
    """Auto-populated per-language JSON translations for a model's designated
    text fields, via auto_translate.py's free GoogleTranslator wrapper (no
    API key or billing). The admin/instructor-entered value is always
    assumed to be written in SOURCE_LANGUAGE (English); on save(), any field
    whose source text has changed (or whose translations don't yet cover
    every active non-source language) gets re-translated. Subclasses must
    define TRANSLATABLE_FIELDS and a `{field}_translations` JSONField for
    each entry.

    Never blocks a save: if the translation call fails for any reason (rate
    limiting, no network, ...), a subclass-defined LOCAL_TRANSLATIONS
    dictionary (if any) fills in whatever it knows about, and anything
    still missing falls back to the English source via `_translated()` --
    retried again on the next save."""
    SOURCE_LANGUAGE = 'en'
    TRANSLATABLE_FIELDS = ()
    # Optional per-model static fallback, keyed by field then by exact
    # English source text: {"name": {"Cybersecurity": {"ar": "..."}}}.
    # Used whenever the translation call is unavailable or fails, so a
    # handful of well-known values (e.g. the seeded track catalog) show up
    # translated immediately instead of waiting on a network round-trip.
    # Live translation results always take priority over this when both are
    # available.
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
        if auto_translate.is_configured():
            try:
                results = auto_translate.translate_fields(pending, target_languages)
            except auto_translate.TranslationError:
                # Never blocks the save -- LOCAL_TRANSLATIONS/English still
                # apply below -- but a failure here used to vanish with no
                # trace anywhere, including in Render's logs. Log it so a
                # persistently-broken network/rate-limit doesn't look
                # identical to "translation just hasn't run yet".
                logger.warning(
                    'Automatic translation failed for %s pk=%s fields=%s -- falling back to '
                    'LOCAL_TRANSLATIONS/English for now.',
                    type(self).__name__, self.pk, list(pending), exc_info=True)
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


class TrackRequest(models.Model):
    """An instructor's request for a new (child) track, gated behind admin
    review -- the same Draft-review-decision shape as Course, but kept as
    its own model rather than a status on Track itself. Track is read by a
    lot of code that has no reason to think about approval state (the nav's
    tracks_menu, the public catalog, CourseCreationForm's track dropdown,
    seed_tracks, sitemaps, ...); folding "pending"/"rejected" into Track
    would mean auditing every one of those to keep an unapproved entry from
    leaking through. A separate table sidesteps that entirely: a request
    that hasn't been approved is not a Track row at all, so it structurally
    cannot appear anywhere a Track normally would. Approving one simply
    creates the real Track (see approve_track_request in views.py) --
    that's the same moment it becomes selectable in course creation.
    """
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending Review')
        APPROVED = 'approved', _('Approved')
        REJECTED = 'rejected', _('Rejected')

    instructor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='track_requests',
        limit_choices_to={'is_instructor': True})
    # The top-level category this new track would nest under -- required,
    # since only a child track (parent__isnull=False) is ever selectable in
    # CourseCreationForm; a request can't become one without a parent.
    parent = models.ForeignKey(
        Track, on_delete=models.CASCADE, related_name='track_requests',
        limit_choices_to={'parent__isnull': True})
    name = models.CharField(max_length=100)
    reason = models.TextField(blank=True, default='', help_text=_('Why this track should exist.'))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True, default='')
    # Set once approved -- the real Track this request became. SET_NULL (not
    # PROTECT/CASCADE) so deleting that Track later doesn't take the request
    # record's own history down with it.
    track = models.ForeignKey(
        Track, on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.get_status_display()})'

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
    # Generated, never uploaded directly -- a 1280x720 composite of
    # thumbnail (or a branded placeholder) with the course title and
    # instructor name burned in. See Course.generate_poster().
    poster_image = models.ImageField(upload_to='course_posters/', blank=True, null=True, editable=False)
    ai_script = models.TextField(help_text=_('The script for Mendoura-produced video generation'), blank=True, null=True)

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

    def total_duration_seconds(self):
        """Sum of every lecture's duration_seconds across every module --
        for course cards/listings. Written as plain Python sums over
        self.modules.all()/module.lectures.all() (not a query annotation)
        so it stays a single query when the caller has
        prefetch_related('modules__lectures') (see _with_stats in
        views.py) -- annotating this in the same call as _with_stats'
        existing Avg('reviews__rating')/Count('enrollments') would silently
        inflate the total via the classic multi-join fan-out problem."""
        return sum(
            lecture.duration_seconds
            for module in self.modules.all()
            for lecture in module.lectures.all()
        )

    def effective_thumbnail_url(self):
        """The manually-uploaded cover photo if there is one, otherwise
        Bunny's auto-generated thumbnail for the first lecture (in module/
        lecture order) that has a Bunny video -- so a course without a
        cover photo shows a real frame from its own content instead of a
        generic placeholder icon. None if neither is available (falls back
        to the placeholder in the template), e.g. BUNNY_PULL_ZONE_HOSTNAME
        isn't configured or the course has no Bunny-hosted video yet."""
        if self.thumbnail:
            return self.thumbnail.url
        for module in self.modules.all():
            for lecture in module.lectures.all():
                if lecture.bunny_video_id:
                    return bunny.thumbnail_url(lecture.bunny_video_id)
        return None

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

    def generate_poster(self):
        """(Re)renders the video-player poster from the current thumbnail
        (or the branded placeholder, if none) and title, and saves it into
        poster_image. Called explicitly by the create/edit-course views
        after every save -- like Certificate.generate_pdf(), this is cheap
        enough (no external API call, pure local compositing) that
        unconditional regeneration is simpler and more reliably correct
        than trying to detect exactly which field changed. Doesn't pick up
        a later change to the instructor's own name -- an accepted, rare
        staleness case, same as a certificate not re-rendering if the
        student later renames their account."""
        from django.core.files.base import ContentFile

        image_bytes = poster.build_poster_image(self)
        self.poster_image.save(f'poster-{self.pk}.jpg', ContentFile(image_bytes), save=True)
        return self.poster_image


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

    class TranscriptStatus(models.TextChoices):
        NOT_STARTED = 'not_started', _('Not started')
        PROCESSING = 'processing', _('Processing')
        DONE = 'done', _('Done')
        FAILED = 'failed', _('Failed')

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
    # ai_generated_script is the one field both Script Only courses' manual
    # scripts AND the "Generate Transcript" button (instructor-triggered,
    # Full Production courses) write into -- the AI Coach's lesson-grounding
    # and the Transcript tab both read this single field regardless of
    # which path populated it, so a Full Production lecture with a
    # generated transcript gets exactly the same treatment as a Script
    # Only one written by hand.
    ai_generated_script = models.TextField(blank=True, null=True)
    transcript_status = models.CharField(
        max_length=20, choices=TranscriptStatus.choices, default=TranscriptStatus.NOT_STARTED)
    transcript_error = models.TextField(blank=True, default='')
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

    def module_is_complete(self, module) -> bool:
        """A module is done once every one of its lectures is completed
        and, if it has a quiz with at least one question, the student has
        a passing QuizAttempt for it. A quiz with zero questions (still
        being built by the instructor) never gates completion."""
        lecture_ids = set(module.lectures.values_list('id', flat=True))
        if lecture_ids:
            completed_ids = set(
                self.lecture_progress.filter(lecture_id__in=lecture_ids, completed=True)
                .values_list('lecture_id', flat=True))
            if completed_ids != lecture_ids:
                return False
        quiz = getattr(module, 'quiz', None)
        if quiz is not None and quiz.questions.exists():
            if not self.quiz_attempts.filter(quiz=quiz, passed=True).exists():
                return False
        return True

    def is_complete(self):
        if self.total_lecture_count() == 0:
            return False
        return all(self.module_is_complete(m) for m in self.course.modules.all())

    def issue_certificate_if_complete(self):
        if self.is_complete():
            certificate, created = Certificate.objects.get_or_create(enrollment=self)
            if created:
                certificate.generate_pdf()
                emails.send_certificate_email(certificate)
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


class Quiz(models.Model):
    module = models.OneToOneField(Module, on_delete=models.CASCADE, related_name='quiz')
    title = models.CharField(max_length=255, blank=True, default='')
    passing_score_percent = models.PositiveSmallIntegerField(
        default=70, validators=[MinValueValidator(1), MaxValueValidator(100)])

    def __str__(self):
        return self.title or f'Quiz for {self.module}'

    @property
    def display_title(self):
        return self.title or f'{self.module.title} Quiz'


class Question(models.Model):
    class QuestionType(models.TextChoices):
        # The only type graded today. New types (e.g. multiple-answer,
        # true/false) are additional choice values here plus a new grading
        # branch in QuizAttempt.grade() -- no schema change required.
        SINGLE_CHOICE = 'single_choice', _('Single choice')

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='questions')
    text = models.TextField()
    question_type = models.CharField(
        max_length=20, choices=QuestionType.choices, default=QuestionType.SINGLE_CHOICE)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.text[:60]


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='choices')
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.text[:60]


class QuizAttempt(models.Model):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name='quiz_attempts')
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='attempts')
    # Set once at creation from the count of this student's prior attempts
    # at this quiz -- frozen after that, same "compute once, never drift"
    # pattern as Payment's split fields. Drives the attempt-3+ answer-reveal
    # rule in the student-facing result view.
    attempt_number = models.PositiveIntegerField(editable=False)
    score_percent = models.DecimalField(max_digits=5, decimal_places=2)
    passed = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.enrollment.student} - {self.quiz} (attempt {self.attempt_number}, {self.score_percent}%)'

    @property
    def reveal_answers(self) -> bool:
        return self.attempt_number >= 3

    def save(self, *args, **kwargs):
        if self._state.adding and self.attempt_number is None:
            prior = QuizAttempt.objects.filter(enrollment=self.enrollment, quiz=self.quiz).count()
            self.attempt_number = prior + 1
        super().save(*args, **kwargs)


class QuizAnswer(models.Model):
    attempt = models.ForeignKey(QuizAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='+')
    selected_choice = models.ForeignKey(Choice, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.attempt} - {self.question}'


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


class LegalDocument(AutoTranslatedFieldsMixin, models.Model):
    """Terms & Conditions / Privacy Policy, etc. English is always the
    source of truth (edited here or via seed_legal_docs); every other
    LANGUAGES entry is populated by the same AI translation pipeline Track
    uses -- never hand-translated."""
    slug = models.SlugField(max_length=50, unique=True)
    title = models.CharField(max_length=200)
    # Optional lead-in shown above the sections, e.g. the Terms' draft/legal
    # review notice. Plain text -- unlike LegalSection.body it's not expected
    # to contain Markdown.
    intro = models.TextField(blank=True, default='')
    last_updated = models.DateField()

    title_translations = models.JSONField(default=dict, blank=True)
    intro_translations = models.JSONField(default=dict, blank=True)
    TRANSLATABLE_FIELDS = ('title', 'intro')

    class Meta:
        ordering = ['slug']

    def __str__(self):
        return self.title

    @property
    def translated_title(self):
        return self._translated('title')

    @property
    def translated_intro(self):
        return self._translated('intro')

    def save(self, *args, **kwargs):
        self._autotranslate()
        super().save(*args, **kwargs)


class LegalSection(AutoTranslatedFieldsMixin, models.Model):
    """One numbered section of a LegalDocument. body is Markdown (this
    project already renders instructor/AI-coach Markdown the same way, via
    python-markdown's 'tables' extension) so a section can include a real
    table -- e.g. the Section 5 revenue-share table -- without ever storing
    raw HTML that an AI translation call could mangle."""
    document = models.ForeignKey(LegalDocument, on_delete=models.CASCADE, related_name='sections')
    order = models.PositiveIntegerField(default=0)
    # Anchor for deep links, e.g. instructor signup linking straight to
    # "#revenue-share". Not translated -- URLs stay stable across languages.
    anchor = models.SlugField(max_length=100, blank=True)
    heading = models.CharField(max_length=255)
    body = models.TextField(help_text=_('Markdown -- supports tables, bold, and lists.'))

    heading_translations = models.JSONField(default=dict, blank=True)
    body_translations = models.JSONField(default=dict, blank=True)
    TRANSLATABLE_FIELDS = ('heading', 'body')

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.document.slug} #{self.order}: {self.heading}'

    @property
    def translated_heading(self):
        return self._translated('heading')

    @property
    def translated_body(self):
        return self._translated('body')

    @property
    def body_html(self):
        import markdown
        return markdown.markdown(self.translated_body, extensions=['fenced_code', 'tables', 'nl2br'])

    def save(self, *args, **kwargs):
        self._autotranslate()
        super().save(*args, **kwargs)
