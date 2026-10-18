import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0030_doc_institution'),
    ]

    operations = [
        migrations.AddField(
            model_name='transfer',
            name='received_codes',
            field=models.TextField(blank=True, default='', help_text='逗号分隔，部分签收时记录已收子集', verbose_name='已签收动物编号'),
        ),
        migrations.AlterField(
            model_name='transfer',
            name='status',
            field=models.CharField(choices=[('pending', '待签收'), ('partial', '部分签收'), ('received', '已签收'), ('rejected', '已驳回'), ('void', '已撤回')], default='pending', max_length=20, verbose_name='状态'),
        ),
    ]
