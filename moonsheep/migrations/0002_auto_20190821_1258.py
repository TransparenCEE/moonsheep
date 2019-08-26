    # Generated by Django 2.2.3 on 2019-08-21 12:58

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('moonsheep', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='priority',
            field=models.DecimalField(decimal_places=2, default=1.0, max_digits=3, validators=[django.core.validators.MaxValueValidator(1.0), django.core.validators.MinValueValidator(0.0)]),
        ),
        migrations.AlterField(
            model_name='task',
            name='state',
            field=models.CharField(choices=[('open', 'open'), ('dirty', 'dirty'), ('checked', 'checked'), ('manual', 'manual')], default='open', max_length=10),
        ),
    ]