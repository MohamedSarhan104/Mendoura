from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from . import views
from .forms import ApprovalAwareAuthenticationForm

urlpatterns = [
    path('', views.platform_home, name='platform_home'),
    path('signup/student/', views.student_signup, name='student_signup'),
    path('signup/instructor/', views.instructor_signup, name='instructor_signup'),
    path('login/', auth_views.LoginView.as_view(
        template_name='registration/login.html',
        authentication_form=ApprovalAwareAuthenticationForm,
    ), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # Password reset -- Django's built-in views, Mendoura's own templates.
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='registration/password_reset_form.html',
        email_template_name='registration/password_reset_email.txt',
        html_email_template_name='registration/password_reset_email.html',
        subject_template_name='registration/password_reset_subject.txt',
        success_url=reverse_lazy('password_reset_done'),
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='registration/password_reset_done.html',
    ), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='registration/password_reset_confirm.html',
        success_url=reverse_lazy('password_reset_complete'),
    ), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='registration/password_reset_complete.html',
    ), name='password_reset_complete'),
    path('profile/', views.profile, name='profile'),
    path('dashboard/instructor/', views.instructor_dashboard, name='instructor_dashboard'),
    path('course/create/', views.create_course, name='create_course'),
    path('course/<int:course_id>/edit/', views.edit_course, name='edit_course'),
    path('course/<int:course_id>/delete/', views.delete_course, name='delete_course'),
    path('courses/', views.course_catalog, name='course_catalog'),
    path('courses/<int:course_id>/', views.course_detail, name='course_detail'),
    path('course/<int:course_id>/toggle-publish/', views.toggle_publish, name='toggle_publish'),
    path('dashboard/admin/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/admin/run-subscription-distribution/', views.run_subscription_distribution,
         name='run_subscription_distribution'),
    path('dashboard/admin/subscription-revenue/', views.admin_subscription_revenue,
         name='admin_subscription_revenue'),

    # Admin interface
    path('dashboard/admin/courses/', views.course_approval_queue, name='course_approval_queue'),
    path('dashboard/admin/courses/<int:course_id>/approve/', views.approve_course, name='approve_course'),
    path('dashboard/admin/courses/<int:course_id>/reject/', views.reject_course, name='reject_course'),
    path('dashboard/admin/users/', views.admin_users, name='admin_users'),
    path('dashboard/admin/users/<int:user_id>/approve/', views.approve_user, name='approve_user'),
    path('dashboard/admin/users/<int:user_id>/reject/', views.reject_user, name='reject_user'),
    path('dashboard/admin/users/<int:user_id>/delete/', views.delete_user, name='delete_user'),
    path('dashboard/admin/payments/', views.admin_payments, name='admin_payments'),
    path('dashboard/admin/payouts/', views.admin_payouts, name='admin_payouts'),
    path('dashboard/admin/payouts/<int:payout_id>/approve/', views.approve_payout, name='approve_payout'),
    path('dashboard/admin/payouts/<int:payout_id>/reject/', views.reject_payout, name='reject_payout'),
    path('dashboard/admin/payouts/<int:payout_id>/paid/', views.mark_payout_paid, name='mark_payout_paid'),
    path('dashboard/admin/tracks/', views.admin_tracks, name='admin_tracks'),
    path('dashboard/admin/tracks/<int:track_id>/toggle/', views.toggle_track_active, name='toggle_track_active'),

    # Instructor interface
    path('course/<int:course_id>/modules/', views.manage_modules, name='manage_modules'),
    path('course/<int:course_id>/modules/<int:module_id>/edit/', views.edit_module, name='edit_module'),
    path('course/<int:course_id>/modules/<int:module_id>/delete/', views.delete_module, name='delete_module'),
    path('course/<int:course_id>/modules/<int:module_id>/lectures/', views.manage_lectures,
         name='manage_lectures'),
    path('lectures/<int:lecture_id>/edit/', views.edit_lecture, name='edit_lecture'),
    path('lectures/<int:lecture_id>/delete/', views.delete_lecture, name='delete_lecture'),
    path('lectures/<int:lecture_id>/create-video/', views.create_bunny_video, name='create_bunny_video'),
    path('lectures/<int:lecture_id>/resources/', views.add_resource, name='add_resource'),
    path('resources/<int:resource_id>/delete/', views.delete_resource, name='delete_resource'),
    path('course/<int:course_id>/students/', views.course_students, name='course_students'),
    path('course/<int:course_id>/submissions/', views.course_submissions, name='course_submissions'),
    path('submissions/<int:submission_id>/grade/', views.grade_submission, name='grade_submission'),
    path('dashboard/wallet/', views.instructor_wallet, name='instructor_wallet'),
    path('dashboard/wallet/payout/', views.request_payout, name='request_payout'),

    # Student interface
    path('tracks/', views.track_list, name='track_list'),
    path('tracks/<slug:slug>/', views.track_detail, name='track_detail'),
    path('search/', views.search_results, name='search_results'),
    path('courses/<int:course_id>/enroll/', views.enroll_course, name='enroll_course'),
    path('courses/<int:course_id>/checkout/', views.checkout_course, name='checkout_course'),
    path('courses/<int:course_id>/review/', views.add_review, name='add_review'),
    path('learning/', views.my_learning, name='my_learning'),
    path('learn/<int:course_id>/<int:lecture_id>/', views.course_player, name='course_player'),
    path('learn/<int:course_id>/<int:lecture_id>/complete/', views.mark_lecture_complete,
         name='mark_lecture_complete'),
    path('learn/<int:course_id>/<int:lecture_id>/watch/', views.record_watch_event,
         name='record_watch_event'),
    path('learn/<int:course_id>/<int:lecture_id>/submit/', views.submit_homework,
         name='submit_homework'),
    path('certificates/<uuid:certificate_uuid>/', views.certificate_view, name='certificate_view'),
    path('certificates/verify/<uuid:certificate_uuid>/', views.certificate_view, name='certificate_verify'),
    path('certificates/<uuid:certificate_uuid>/download/', views.certificate_download, name='certificate_download'),
    path('dashboard/ai-coach/', views.ai_coach, name='ai_coach'),
    path('dashboard/ai-coach/send/', views.ai_coach_send, name='ai_coach_send'),
]