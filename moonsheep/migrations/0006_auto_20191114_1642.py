# Generated by Django 2.2.5 on 2019-11-14 16:42

from django.db import migrations

def copy_json_fields(apps, schema_editor):
    # We can't import the Person model directly as it may be a newer
    # version than this migration expects. We use the historical version.
    Task = apps.get_model('moonsheep', 'Task')
    for t in Task.objects.all():
        t.params2 = t.params
        t.save()

    Entry = apps.get_model('moonsheep', 'Entry')
    for e in Entry.objects.all():
        e.data2 = e.data
        e.save()

class Migration(migrations.Migration):

    dependencies = [
        ('moonsheep', '0005_auto_20191114_1640'),
    ]

    operations = [
        migrations.RunPython(copy_json_fields),
    ]