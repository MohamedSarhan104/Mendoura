from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, Track, TrackRoadmapStep, Course, Module, Lecture, Resource, Submission,
    Payment, Enrollment, LectureProgress, InstructorWallet, WalletTransaction,
    Payout, Plan, Subscription, SubscriptionPeriod, RevenueDistribution, WatchEvent,
    Review, Certificate,
)


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'is_student', 'is_instructor', 'is_staff')
    list_filter = ('is_student', 'is_instructor', 'is_staff')
    fieldsets = UserAdmin.fieldsets + (
        ('LMS Info', {'fields': ('is_student', 'is_instructor', 'phone_number')}),
    )


class TrackRoadmapStepInline(admin.TabularInline):
    model = TrackRoadmapStep
    extra = 1
    fields = ('order', 'title', 'course', 'is_optional')


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'order', 'is_active')
    list_filter = ('is_active', 'parent')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [TrackRoadmapStepInline]


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('title', 'instructor', 'track', 'level', 'production_type',
                     'price', 'status', 'created_at')
    list_filter = ('status', 'production_type', 'level', 'track')
    search_fields = ('title', 'instructor__username')
    prepopulated_fields = {'slug': ('title',)}


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ('title', 'course', 'order')
    list_filter = ('course',)


@admin.register(Lecture)
class LectureAdmin(admin.ModelAdmin):
    list_display = ('title', 'module', 'content_type', 'is_preview', 'order')
    list_filter = ('content_type', 'is_preview')


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'lecture', 'file_size')


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('student', 'lecture', 'grade', 'submitted_at')
    list_filter = ('lecture__module__course',)
    search_fields = ('student__username',)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('student', 'course', 'total_amount', 'instructor_amount',
                     'platform_amount', 'status', 'created_at')
    list_filter = ('status', 'production_type_at_purchase')
    search_fields = ('student__username', 'course__title', 'provider_transaction_id')


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ('student', 'course', 'via_subscription', 'enrolled_at')
    list_filter = ('course', 'via_subscription')
    search_fields = ('student__username', 'course__title')


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'interval', 'price_egp', 'price_usd', 'duration_days', 'is_active')
    list_filter = ('is_active', 'interval')


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('student', 'plan', 'status', 'amount_paid', 'currency', 'started_at', 'expires_at')
    list_filter = ('status', 'plan')
    search_fields = ('student__username', 'provider_transaction_id')


@admin.register(SubscriptionPeriod)
class SubscriptionPeriodAdmin(admin.ModelAdmin):
    list_display = ('subscription', 'period_start', 'period_end', 'amount_paid', 'status', 'distributed_at')
    list_filter = ('status',)


@admin.register(RevenueDistribution)
class RevenueDistributionAdmin(admin.ModelAdmin):
    list_display = ('period', 'course', 'instructor', 'seconds_watched',
                     'instructor_amount', 'platform_amount', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('instructor__username', 'course__title')


@admin.register(WatchEvent)
class WatchEventAdmin(admin.ModelAdmin):
    list_display = ('student', 'lecture', 'course', 'seconds_watched', 'occurred_at')
    list_filter = ('occurred_at',)
    search_fields = ('student__username', 'course__title')


@admin.register(LectureProgress)
class LectureProgressAdmin(admin.ModelAdmin):
    list_display = ('enrollment', 'lecture', 'completed', 'completed_at')
    list_filter = ('completed',)


@admin.register(InstructorWallet)
class InstructorWalletAdmin(admin.ModelAdmin):
    list_display = ('instructor', 'total_earnings', 'available_balance',
                     'pending_balance', 'total_withdrawn')


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'type', 'amount', 'balance_after', 'created_at')
    list_filter = ('type',)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ('wallet', 'amount', 'status', 'requested_at', 'processed_at')
    list_filter = ('status',)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('student', 'course', 'rating', 'created_at')
    list_filter = ('rating',)


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = ('enrollment', 'uuid', 'issued_at')
