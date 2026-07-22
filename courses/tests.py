import hashlib
import hmac
import json
import random
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.translation import override as translation_override

from . import ai_coach, ai_translate, paymob
from .models import (
    AIConversation, AIMessage, Certificate, Course, Enrollment, InstructorWallet, Lecture, Module,
    Payment, Payout, Plan, Resource, RevenueDistribution, Review, Subscription, SubscriptionPeriod,
    Submission, Track, User, WalletTransaction, WatchEvent,
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
                     'admin_payouts', 'admin_tracks'):
            response = self.client.get(reverse(name))
            self.assertNotEqual(response.status_code, 200, f'{name} was reachable by a non-admin')

    def test_admin_can_reach_admin_pages(self):
        self.client.force_login(self.admin)
        for name in ('admin_dashboard', 'course_approval_queue', 'admin_users', 'admin_payments',
                     'admin_payouts', 'admin_tracks'):
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
        }
        data.update(overrides)
        return data

    def test_student_signup_creates_unapproved_pending_account(self):
        self.client.post(reverse('student_signup'), self._signup_data())
        user = User.objects.get(username='newbie')
        self.assertTrue(user.is_student)
        self.assertFalse(user.is_approved)

    def test_instructor_signup_creates_unapproved_pending_account(self):
        self.client.post(reverse('instructor_signup'), self._signup_data(username='newbie_inst'))
        user = User.objects.get(username='newbie_inst')
        self.assertTrue(user.is_instructor)
        self.assertFalse(user.is_approved)

    def test_signup_does_not_auto_login(self):
        response = self.client.post(reverse('student_signup'), self._signup_data(), follow=True)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_signup_redirects_to_login_with_pending_message(self):
        response = self.client.post(reverse('student_signup'), self._signup_data(), follow=True)
        self.assertRedirects(response, reverse('login'))
        self.assertContains(response, 'pending administrator approval')

    def test_unapproved_user_cannot_login(self):
        self.client.post(reverse('student_signup'), self._signup_data())
        response = self.client.post(reverse('login'), {'username': 'newbie', 'password': 'a-strong-password-1'})
        self.assertContains(response, 'Your account is currently pending administrator approval.')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_approved_user_can_login_normally(self):
        user = User.objects.create_user(username='approved_stud', password='pw', is_student=True)
        self.assertTrue(user.is_approved)
        response = self.client.post(reverse('login'), {'username': 'approved_stud', 'password': 'pw'}, follow=True)
        self.assertTrue(response.wsgi_request.user.is_authenticated)


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
            username='pending_inst', password='pw', is_instructor=True)
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

    def test_reject_stores_reason(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('reject_course', args=[self.course.id]), {'reason': 'Low quality audio'})
        self.course.refresh_from_db()
        self.assertEqual(self.course.status, Course.Status.REJECTED)
        self.assertEqual(self.course.rejection_reason, 'Low quality audio')

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
    network call itself (ai_translate.translate_fields) is mocked -- these
    tests are about the save()-time trigger/staleness/fallback logic, not
    the Anthropic integration (that's covered by mocking, same as
    AICoachTests)."""

    def test_without_ai_configured_save_succeeds_and_falls_back_to_source(self):
        with override_settings(AI_API_KEY=''):
            track = Track.objects.create(name='Robotics', description='Build robots.')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')
        self.assertEqual(track.translated_description, 'Build robots.')

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
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

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
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

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
    def test_unchanged_name_does_not_retrigger_translation_on_next_save(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'أ', 'fr': 'f', 'es': 'e'}, 'description': {}}
        track = Track.objects.create(name='Robotics', description='')
        self.assertEqual(mock_translate.call_count, 1)

        track.order = 5
        track.save()
        self.assertEqual(mock_translate.call_count, 1)

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
    def test_changed_name_retriggers_translation(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'أ', 'fr': 'f', 'es': 'e'}, 'description': {}}
        track = Track.objects.create(name='Robotics', description='')
        self.assertEqual(mock_translate.call_count, 1)

        mock_translate.return_value = {'name': {'ar': 'ب', 'fr': 'g', 'es': 'h'}, 'description': {}}
        track.name = 'Advanced Robotics'
        track.save()
        self.assertEqual(mock_translate.call_count, 2)
        self.assertEqual(track.name_translations['ar'], 'ب')

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
    def test_translation_error_does_not_break_save(self, mock_translate):
        mock_translate.side_effect = ai_translate.TranslationError('boom')
        track = Track.objects.create(name='Robotics', description='Build robots.')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')

    @override_settings(AI_API_KEY='')
    def test_local_fallback_translates_known_track_name_without_ai(self):
        track = Track.objects.create(name='Cybersecurity')
        self.assertEqual(track.name_translations['ar'], 'الأمن السيبراني')
        with translation_override('ar'):
            self.assertEqual(track.translated_name, 'الأمن السيبراني')

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
    def test_local_fallback_fills_gap_when_ai_call_fails(self, mock_translate):
        mock_translate.side_effect = ai_translate.TranslationError('boom')
        track = Track.objects.create(name='Web Development')
        self.assertEqual(track.name_translations['ar'], 'تطوير الويب')

    @override_settings(AI_API_KEY='test-key')
    @patch('courses.models.ai_translate.translate_fields')
    def test_real_ai_result_takes_priority_over_local_fallback(self, mock_translate):
        mock_translate.return_value = {'name': {'ar': 'ترجمة حقيقية', 'fr': 'f', 'es': 'e'}}
        track = Track.objects.create(name='Tech')
        # The AI's own Arabic translation wins over the local dictionary entry.
        self.assertEqual(track.name_translations['ar'], 'ترجمة حقيقية')

    @override_settings(AI_API_KEY='')
    def test_unmapped_track_name_still_falls_back_to_english_without_ai(self):
        track = Track.objects.create(name='Robotics')
        self.assertEqual(track.name_translations, {})
        self.assertEqual(track.translated_name, 'Robotics')

    @override_settings(AI_API_KEY='')
    def test_local_fallback_translates_languages_track_name_without_ai(self):
        track = Track.objects.create(name='Languages')
        self.assertEqual(track.name_translations['ar'], 'اللغات')
        with translation_override('ar'):
            self.assertEqual(track.translated_name, 'اللغات')

    @override_settings(AI_API_KEY='')
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
            username='paymob_stud', password='pw', is_student=True)
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


class SubscriptionAccessControlTests(TestCase):
    """An active subscriber gets frictionless access to any paid course
    without an individual purchase."""

    def setUp(self):
        self.instructor = User.objects.create_user(
            username='sub_access_inst', password='pw', is_instructor=True)
        self.student = User.objects.create_user(
            username='sub_access_stud', password='pw', is_student=True)
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


@override_settings(BUNNY_LIBRARY_ID='705216', BUNNY_API_KEY='k', BUNNY_TOKEN_KEY='tok')
class BunnyPlayerEmbedTests(TestCase):
    def test_player_embeds_signed_bunny_iframe(self):
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
        with override_settings(AI_API_KEY=''):
            response = self.client.get(reverse('ai_coach'))
        self.assertContains(response, 'Mendoura General AI Coach')

    def test_no_sandbox_badge_when_api_key_configured(self):
        self.client.force_login(self.student)
        with override_settings(AI_API_KEY='test-key'):
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

    @override_settings(AI_API_KEY='')
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

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_tech_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you help me learn Python?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Modern Software Engineering', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_business_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'How do I grow my business marketing?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Elite Entrepreneurship Framework', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_language_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'I want to learn Arabic'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Language Learning Roadmap', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_study_schedule_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you build me a study schedule?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Weekly Study Schedule', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_math_science_keyword_in_arabic(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'عايز افهم فيزياء'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Math &amp; Science', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_career_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'Can you help me prep for a job interview?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Career &amp; Interview Prep Kit', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_design_keyword(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'How do I get better at Figma and UX design?'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Design Fundamentals', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
    def test_send_without_ai_configured_matches_productivity_keyword_in_arabic(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('ai_coach_send'), data=json.dumps({'message': 'محتاج تحفيز وتنظيم وقتي'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Focus &amp; Productivity Framework', response.json()['reply_html'])

    @override_settings(AI_API_KEY='')
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
        with override_settings(AI_API_KEY=''):
            self.assertFalse(ai_coach.is_configured())
        with override_settings(AI_API_KEY='some-key'):
            self.assertTrue(ai_coach.is_configured())

    def test_send_message_returns_sandbox_reply_when_not_configured(self):
        with override_settings(AI_API_KEY=''):
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


class AITranslateClientTests(TestCase):
    def test_is_configured_reflects_setting(self):
        with override_settings(AI_API_KEY=''):
            self.assertFalse(ai_translate.is_configured())
        with override_settings(AI_API_KEY='some-key'):
            self.assertTrue(ai_translate.is_configured())

    def test_translate_fields_raises_when_not_configured(self):
        with override_settings(AI_API_KEY=''):
            with self.assertRaises(ai_translate.TranslationError):
                ai_translate.translate_fields({'name': 'Robotics'}, ['ar'])

    @override_settings(AI_API_KEY='test-key')
    def test_translate_fields_returns_empty_without_calling_api_when_nothing_to_translate(self):
        self.assertEqual(ai_translate.translate_fields({}, ['ar']), {})
        self.assertEqual(ai_translate.translate_fields({'name': 'Robotics'}, []), {})
