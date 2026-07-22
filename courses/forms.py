from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.utils.translation import gettext_lazy as _
from .models import (
    User, Course, InstructorWallet, Lecture, Module, Resource, Submission, Track,
    Review, Payout,
)

INPUT_CLASSES = 'w-full px-4 py-3 rounded-lg border border-gray-300 dark:border-gray-700 bg-transparent focus:ring-2 focus:ring-brand-500 outline-none'


class DuplicateGuardMixin:
    """Friendly, field-specific duplicate errors for signup forms. Username
    uniqueness is already enforced by UserCreationForm/the model field;
    email and phone_number aren't unique at the DB level yet, so this is a
    form-level check only -- a race between two simultaneous signups could
    still both pass validation. Good enough for now, but not the same
    guarantee a DB constraint would give."""

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if username and User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_('An account with this username already exists.'))
        return username

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_('An account with this email already exists.'))
        return email

    def clean_phone_number(self):
        phone = (self.cleaned_data.get('phone_number') or '').strip()
        if phone and User.objects.filter(phone_number=phone).exists():
            raise forms.ValidationError(_('An account with this phone number already exists.'))
        return phone


# 1. Student Registration Form
class StudentSignUpForm(DuplicateGuardMixin, UserCreationForm):
    phone_number = forms.CharField(
        max_length=15, required=False,
        widget=forms.TextInput(attrs={'placeholder': _('Phone Number'), 'class': INPUT_CLASSES})
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'phone_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', INPUT_CLASSES)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_student = True
        user.is_approved = False
        if commit:
            user.save()
        return user


# 2. Instructor Registration Form
class InstructorSignUpForm(DuplicateGuardMixin, UserCreationForm):
    phone_number = forms.CharField(
        max_length=15, required=True,
        widget=forms.TextInput(attrs={'placeholder': _('Phone Number'), 'class': INPUT_CLASSES})
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'phone_number')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', INPUT_CLASSES)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_instructor = True
        user.is_approved = False
        if commit:
            user.save()
            InstructorWallet.objects.get_or_create(instructor=user)
        return user


# Used by the login view (see urls.py) instead of the default
# AuthenticationForm so a pending signup gets a clear, specific reason for
# the rejected login instead of Django's generic "inactive account" copy.
class ApprovalAwareAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_approved:
            raise forms.ValidationError(
                _('Your account is currently pending administrator approval.'),
                code='pending_approval',
            )


# 3. Course Creation Form
# Legible on both light and dark backgrounds -- the previous version had no
# explicit text color at all, so typed input inherited whatever gray the
# surrounding page set, which in dark mode was nearly unreadable against the
# field background. text-base (16px) also avoids iOS Safari's auto-zoom on
# focus for anything smaller.
COURSE_FORM_INPUT_CLASSES = (
    'w-full border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 rounded-lg p-3 '
    'text-base leading-relaxed font-medium text-[#1e293b] dark:text-[#f5f5f5] '
    'placeholder:font-normal placeholder:text-gray-400 dark:placeholder:text-gray-500 '
    'focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none'
)


COURSE_LANGUAGE_CHOICES = [
    ('English', _('English')),
    ('Arabic', _('Arabic')),
    ('French', _('French')),
    ('German', _('German')),
    ('Spanish', _('Spanish')),
    ('Italian', _('Italian')),
    ('Turkish', _('Turkish')),
]


class CourseCreationForm(forms.ModelForm):
    language = forms.ChoiceField(choices=COURSE_LANGUAGE_CHOICES, initial='English',
                                  widget=forms.Select(attrs={'class': COURSE_FORM_INPUT_CLASSES}))

    class Meta:
        model = Course
        fields = ['title', 'description', 'track', 'level', 'language', 'production_type',
                  'price', 'is_free', 'thumbnail', 'ai_script']
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': _('e.g. Introduction to Python'),
                'class': COURSE_FORM_INPUT_CLASSES,
            }),
            'description': forms.Textarea(attrs={
                'rows': 4, 'placeholder': _('What is this course about?'),
                'class': COURSE_FORM_INPUT_CLASSES,
            }),
            'track': forms.Select(attrs={'class': COURSE_FORM_INPUT_CLASSES}),
            'level': forms.Select(attrs={'class': COURSE_FORM_INPUT_CLASSES}),
            'production_type': forms.RadioSelect(),
            'price': forms.NumberInput(attrs={
                'placeholder': '0.00',
                'class': COURSE_FORM_INPUT_CLASSES,
            }),
            'thumbnail': forms.ClearableFileInput(attrs={
                'class': f'{COURSE_FORM_INPUT_CLASSES} p-2',
            }),
            'ai_script': forms.Textarea(attrs={
                'rows': 8,
                'placeholder': _('Type the script here. Our AI will turn this text into a professional video lecture.'),
                'class': COURSE_FORM_INPUT_CLASSES,
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only leaf tracks take courses -- a parent category (e.g. "Tech")
        # has no course list of its own, so a course filed under one would
        # never surface on any student-facing browse page.
        self.fields['track'].queryset = Track.objects.filter(is_active=True, parent__isnull=False)


# Review Form (enrolled students only, enforced in the view)
class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['rating', 'comment']
        widgets = {
            'rating': forms.Select(choices=[(i, i) for i in range(1, 6)], attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'comment': forms.Textarea(attrs={
                'rows': 3, 'placeholder': _('Share your experience with this course...'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# 5. Submission Form (Student uploads homework)
class SubmissionForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = ['submitted_file', 'submission_link', 'note']
        widgets = {
            'submitted_file': forms.ClearableFileInput(attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 rounded-lg p-3'
            }),
            'submission_link': forms.URLInput(attrs={
                'placeholder': _('Optional: Google Drive / GitHub link'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'note': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': _('Any notes for your instructor?'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# Grade Form (Instructor grades a student's homework Submission)
class GradeForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = ['grade', 'feedback']
        widgets = {
            'grade': forms.TextInput(attrs={
                'placeholder': _('e.g. 18/20 or A-'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'feedback': forms.Textarea(attrs={
                'rows': 3, 'placeholder': _('Feedback for the student...'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# Track Form (Admin CRUD)
class TrackForm(forms.ModelForm):
    class Meta:
        model = Track
        fields = ['parent', 'name', 'description', 'icon', 'order']
        widgets = {
            'parent': forms.Select(attrs={'class': INPUT_CLASSES}),
            'name': forms.TextInput(attrs={'class': INPUT_CLASSES, 'placeholder': _('e.g. Web Development')}),
            'description': forms.Textarea(attrs={'class': INPUT_CLASSES, 'rows': 2}),
            'icon': forms.TextInput(attrs={'class': INPUT_CLASSES, 'placeholder': 'fa-laptop-code'}),
            'order': forms.NumberInput(attrs={'class': INPUT_CLASSES}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['parent'].queryset = Track.objects.filter(parent__isnull=True)
        self.fields['parent'].required = False


# Module Form (Instructor organizes course into sections)
class ModuleForm(forms.ModelForm):
    class Meta:
        model = Module
        fields = ['title', 'order']
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': _('e.g. Getting Started'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'order': forms.NumberInput(attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# Resource Form (Instructor attaches downloadable files to a lecture)
class ResourceForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = ['title', 'file']
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': _('e.g. Slides.pdf'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'file': forms.ClearableFileInput(attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 rounded-lg p-3'
            }),
        }


# Payout Request Form (Instructor withdraws from available balance)
class PayoutRequestForm(forms.ModelForm):
    class Meta:
        model = Payout
        fields = ['amount', 'method']
        widgets = {
            'amount': forms.NumberInput(attrs={
                'placeholder': '0.00',
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'method': forms.TextInput(attrs={
                'placeholder': _('e.g. Bank transfer, PayPal'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# 4. Lecture Form (Instructor uploads video/materials)
class LectureForm(forms.ModelForm):
    # video_file (direct upload) is intentionally gone -- videos now upload to
    # Bunny Stream straight from the browser (see edit_lecture). video_url
    # stays for the occasional externally-hosted embed.
    class Meta:
        model = Lecture
        fields = ['title', 'video_url', 'is_preview', 'accepts_submission', 'order']
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': _('e.g. Introduction to Variables'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'video_url': forms.URLInput(attrs={
                'placeholder': _('https://youtube.com/... (external embed, optional)'),
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
            'order': forms.NumberInput(attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 bg-transparent rounded-lg p-3 focus:ring-2 focus:ring-brand-500 outline-none'
            }),
        }


# Profile Form (any authenticated user updates their own avatar)
class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['avatar']
        widgets = {
            'avatar': forms.ClearableFileInput(attrs={
                'class': 'w-full border border-gray-300 dark:border-gray-700 rounded-lg p-3'
            }),
        }
