# Generated by Django 3.2.14 on 2022-08-31 10:26

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('confreg', '0089_speaker_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='conferencetweetqueue',
            name='comment',
            field=models.CharField(blank=True, max_length=200, verbose_name='Internal comment'),
        ),
    ]
