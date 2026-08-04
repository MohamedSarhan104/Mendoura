from django.db import migrations


def approve_pending_students(apps, schema_editor):
    # Student signups no longer go through admin review (StudentSignUpForm
    # stopped setting is_approved=False -- only instructor applications
    # still require it), so any student created before this change who is
    # still stuck pending would otherwise never log in again. One-time
    # catch-up; does not touch pending instructors.
    User = apps.get_model('courses', 'User')
    User.objects.filter(is_student=True, is_approved=False).update(is_approved=True)


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0024_alter_user_username'),
    ]

    operations = [
        migrations.RunPython(approve_pending_students, migrations.RunPython.noop),
    ]
