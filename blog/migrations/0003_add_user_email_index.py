from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0002_add_post_indexes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.CharField(db_index=True, max_length=255),
        ),
    ]
