from django.db import migrations


def wipe_courses(apps, schema_editor):
    Course = apps.get_model('courses', 'Course')
    Course.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0003_lecture_attachment_lecture_video_file_submission'),
    ]

    operations = [
        migrations.RunPython(wipe_courses, migrations.RunPython.noop),
    ]
