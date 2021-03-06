import os
import urllib.parse

from django.template import Library
from django.template.defaultfilters import stringfilter
from django.urls import reverse

from moonsheep.statistics import stats_documents_verified, stats_users

register = Library()


@register.inclusion_tag('token.html')
def moonsheep_token(task: 'AbstractTask'):
    return {
        'task_id': task.instance.id,
        'task_type': task.name
    }


@register.simple_tag
def document_change_url(instance):
    meta = type(instance)._meta
    view_name = 'admin:%s_%s_change' % (meta.app_label, meta.model_name)
    return reverse(view_name, args=(instance.id,))


@register.filter
@stringfilter
def task_name(value):
    return value.split('.').pop()


@register.filter
@stringfilter
def pretty_url(value):
    return urllib.parse.unquote(os.path.basename(value))


register.simple_tag(stats_documents_verified)

register.simple_tag(stats_users)
