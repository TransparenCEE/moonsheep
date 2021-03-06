import os
import random
import re
import tempfile
from typing import Sequence

import dpath.util
from django.contrib import messages
from django.contrib.auth import login
from django.db import IntegrityError
from django.http import HttpResponseRedirect, Http404, FileResponse
from django.http.request import QueryDict
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _
from django.views import View
from django.views.generic import FormView, TemplateView

from moonsheep.exporters import Exporter
from moonsheep.exporters.exporters import FileExporter
from moonsheep.importers.importers import IDocumentImporter
from moonsheep.mapper import klass_from_name
from moonsheep.users import UserRequiredMixin, generate_nickname
from . import registry
from .exceptions import (
    PresenterNotDefined, NoTasksLeft, TaskMustSetTemplate)
from .models import Task, Entry, User
from .settings import MOONSHEEP
from .tasks import AbstractTask


class TaskView(UserRequiredMixin, FormView):
    task_type: AbstractTask = None
    template_name: str = None
    form_class = None
    error_message = None
    error_template = None

    def get(self, request, *args, **kwargs):
        """
        Returns form for this task

        Algorithm:
        1. Get actual (implementing) class name, ie. FindTableTask
        2. Derive template name for it and try to return if exists 'forms/find_table.html'
        3. Otherwise return `forms/FindTableForm`
        4. Otherwise return error suggesting to implement 2 or 3
        :return: path to the template (string) or Django's Form class
        """
        try:
            self.task_type = self._get_task()
            self.configure_template_and_form()
        except NoTasksLeft:  # TODO test case for project
            self.error_message = 'Task Chooser returned no tasks'
            self.error_template = 'error-messages/no-tasks.html'
            self.task_type = None
            self.template_name = 'error-messages/no-tasks.html'  # TODO we do not want to define the main err template in Moonsheep. How to generally handle messages and errors?
        except PresenterNotDefined:
            self.error_message = 'Presenter not defined'
            self.error_template = 'error-messages/presenter-not-defined.html'
            self.task_type = None
            self.template_name = 'error-messages/presenter-not-defined.html'  # TODO we do not want to main err template it in Moonsheep. How to generally handle messages and errors?

        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        """
        Handle POST requests: instantiate a form instance with the passed
        POST variables and then check if it's valid.

        Overrides django.views.generic.edit.ProcessFormView to adapt for a case
        when user hasn't defined form for a given task.
        """
        if '_task_id' not in request.POST:
            raise KeyError('Missing _task_id field. Include moonsheep_token template tag!')

        if '_task_type' not in request.POST:
            return KeyError('Missing _task_type field. Include moonsheep_token template tag!')

        # TODO keep task_id separate
        self.task_type = self._get_task(request.POST['_task_id'])
        self.configure_template_and_form()

        form = self.get_form()

        # no form defined in the task, no field validation then so we just save the entry
        if form is None:
            data = unpack_post(request.POST)
            # TODO what to do if we have forms defined? is Django nested formset a way to go?
            # Check https://stackoverflow.com/questions/20894629/django-nested-inline-formsets
            # Check https://docs.djangoproject.com/en/2.0/ref/contrib/admin/#django.contrib.admin.InlineModelAdmin
            self._save_entry(self.request.POST['_task_id'], data)
            return HttpResponseRedirect(self.get_success_url())

        # there is a task's form defined, validate fields with it
        if form.is_valid():
            # and if is valid entry will be saved
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'project_id': 'fake it'  # TODO remove it everywhere
        })
        if self.task_type:
            context['task'] = self.task_type
            try:
                context['presenter'] = self.task_type.get_presenter()
            except TypeError:
                raise PresenterNotDefined
        else:
            context.update({
                'error': True,
                'message': self.error_message,
                'template': self.error_template
            })
        return context

    def configure_template_and_form(self):
        # TODO maybe merge with _get_task (but check if it work for tests)
        # Template showing a task: presenter and the form, can be overridden by setting task_template in your Task
        # By default it uses moonsheep/templates/task.html

        # Overriding template
        if hasattr(self.task_type, 'template_name'):
            self.template_name = self.task_type.template_name

        if not self.template_name:
            raise TaskMustSetTemplate(self.task_type.__class__)

        self.form_class = getattr(self.task_type, 'task_form', None)

    # =====================
    # Override FormView to adapt for a case when user hasn't defined form for a given task
    # and to process form in our own manner

    def get_form_class(self):
        return self.task_type.task_form if hasattr(self.task_type, 'task_form') else None

    def get_form(self, form_class=None):
        """Return an instance of the form to be used in this view.

        Overrides django.views.generic.edit.FormMixin to adapt for a case
        when user hasn't defined form for a given task.
        """
        if form_class is None:
            form_class = self.get_form_class()

        if form_class is None:
            return None
        return form_class(**self.get_form_kwargs())

    def form_valid(self, form):
        self._save_entry(self.request.POST['_task_id'], form.cleaned_data)

        return super(TaskView, self).form_valid(form)

    # End of FormView override
    # ========================

    def _get_task(self, task_id: str = None) -> AbstractTask:
        """
        Mechanism responsible for getting a task send data for

        :rtype: AbstractTask
        :return: user's implementation of AbstractTask object
        """
        if MOONSHEEP['DEV_ROTATE_TASKS']:
            return self.get_random_mocked_task_data(task_id)

        if task_id is not None:
            task = Task.objects.get(pk=task_id)

        else:
            # Choose a task to serve to user
            task = self.choose_a_task()

        return AbstractTask.create_task_instance(task)

    __mocked_task_counter = 0

    # TODO rename after choosing a convention
    def get_random_mocked_task_data(self, task_type: str = None) -> AbstractTask:
        # Make sure that tasks are imported before this code is run, ie. in your project urls.py

        # Allow to test one type definition, by passing it as GET parameter
        if task_type is None:
            task_type = self.request.GET.get('task_type', None)

        if task_type is None:
            defined_tasks = registry.TASK_TYPES

            if not defined_tasks:
                raise NotImplementedError(
                    "You haven't defined any tasks or forgot to add in urls.py folllowing line: from .tasks import *"
                    + "# Keep it to make Moonsheep aware of defined tasks")

            # Rotate tasks one after another
            TaskView.__mocked_task_counter += 1
            if TaskView.__mocked_task_counter >= len(defined_tasks):
                TaskView.__mocked_task_counter = 0
            task_type = defined_tasks[TaskView.__mocked_task_counter]

        task_class = klass_from_name(task_type)

        # Developers should provide mocked params for the task
        has_mocked_params = False
        try:
            if hasattr(task_class, 'mocked_params'):
                has_mocked_params = True
        except TypeError:
            pass

        if not has_mocked_params:
            raise NotImplementedError(
                "Task {} should define '@classproperty def mocked_params(cls) -> dict:'".format(task_type))

        task = AbstractTask.create_task_instance(Task(type=task_type, id=task_type, params=task_class.mocked_params))

        return task

    def choose_a_task(self) -> Task:
        """
        Choose a task to be served to user

        By default it select 20 open tasks that user didn't contribute to and chooses one randomly
        """
        # TODO make it pluggable / create interface for it
        # TODO implement priority setting (how to set priority for the imported task?)

        # TODO test exclude works properly
        tasks = Task.objects.filter(state=Task.OPEN).exclude(entry__user=self.request.user).order_by('-priority')[:20]

        if not tasks:
            raise NoTasksLeft()

        # choose task at random from the top 20, so everyone won't get the same task
        # TODO otherwise an "open_count" could help to limit it,
        #  especially where there are a lot of volunteers and long tasks
        return random.choice(tasks)

    def _save_entry(self, task_id, data) -> None:
        """
        Save entry in the database and run the crosschecking
        """
        if MOONSHEEP['DEV_ROTATE_TASKS']:
            # In this mode we don't want to create entries
            # We rather skip directly to saving models assuming that one entry is all we need for crosscheck
            # Warning: This does not test if cross-checking is working properly.
            # TODO How to test cross-checking?
            # TODO shouldn't two method below be one?
            self.task_type.save_verified_data(data)

            # create new tasks
            self.task_type.after_save(data)
            return

        # Create new entry
        Entry(task_id=task_id, user=self.request.user, data=data).save()

        # Run verification, saving, progress updates
        self.task_type.verify_and_save(task_id)

        messages.add_message(self.request, messages.SUCCESS, _(
            'Thank you! Are you ready for a next task? Or {linkopen}take a pause?{linkclose}').format(
            linkopen='<a class="finish-transcription" href="' + reverse('finish-transcription') + '">',
            linkclose='</a>'))

    def _get_user_ip(self):
        return self.request.META.get(
            'HTTP_X_FORWARDED_FOR', self.request.META.get('REMOTE_ADDR')
        ).split(',')[-1].strip()


class ManualVerificationView(TaskView):
    def _get_task(self, task_id: str = None) -> AbstractTask:
        # We know the task that we want to serve
        return super()._get_task(self.kwargs['task_id'])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get all entries and their confidence to support moderator's decision
        entries: Sequence[Entry] = self.task_type.instance.entry_set.all()

        # Repack them as options for each field
        fields = {}
        for e in entries:
            for fld, value in e.data.items():
                values = fields.get(fld, set())
                values.add(value)
                fields[fld] = values

        for fld, values in fields.items():
            fields[fld] = list(values)

        context.update({
            'entries_data': fields
        })

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST['_action']

        if action == 'cancel':
            return HttpResponseRedirect(reverse('ms-admin'))

        elif action == 'skip':
            return HttpResponseRedirect(self.get_success_url(request.POST['_task_id']))

        # action == save, proceed via super.post() to overriden _save_entry defined below
        return super().post(request, *args, **kwargs)

    def _save_entry(self, task_id, data) -> None:
        """
        Override default save_entry

        :param task_id:
        :param data:
        :return:
        """

        # Create new entry
        e = Entry(task_id=task_id, user=self.request.user, data=data, closed_manually=True)
        # TODO don't crosscheck entries that have been manually checked, we might accidentally overwrite data
        e.save()

        # Run verification, saving, progress updates
        self.task_type.verified_manually(task_id, e)

        messages.add_message(self.request, messages.SUCCESS, _('Thank you! We saved your entry as verified!'))

    def get_success_url(self, after_task: int = None):
        """
        Get next dirty task if possible
        :return:
        """

        if after_task:
            task = Task.objects.next_dirty(after_task)
        else:
            task = Task.objects.dirty().only('id').first()

        if task:
            return reverse('ms-manual-verification', kwargs={'task_id': task.id})

        else:
            # TODO message no more tasks
            return reverse('ms-admin')


# TODO separate views/admin
class DocumentListView(TemplateView):
    template_name = 'moonsheep/documents.html'

    def get_context_data(self, **kwargs):
        documents = registry.get_document_model().objects.all().order_by('-progress')
        importers = IDocumentImporter.implementations()

        context = super().get_context_data(**kwargs)
        context.update({
            # TODO paging, etc.
            'documents': documents,
            'importers': importers
        })

        get_doc_details = self.request.GET.get('details_of', None)
        if get_doc_details:
            # Get progress of all tasks and their subtasks

            get_doc_details = int(get_doc_details)

            tasks = Task.objects.filter(doc_id=get_doc_details).order_by('id')

            nodes = {None: {'children': []}}
            for t in tasks:
                node = {
                    "task": t,
                    "children": []
                }
                # Register node
                nodes[t.id] = node
                # Add it to parent node
                nodes[t.parent_id]['children'].append(node)

            context.update({
                'progress_tree': nodes[None],
                'details_doc_id': get_doc_details
            })

        return context


class CampaignView(TemplateView):
    template_name = 'moonsheep/campaign.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        dirty_tasks = Task.objects.dirty()[:5]

        context.update({
            'dirty_tasks': dirty_tasks,

            'exporters':
            # all exporting files
                [{
                    'url': reverse('ms-export', args=[slug]),
                    'label': 'Download ' + (getattr(cls, 'label', None) or slug.upper())
                } for slug, cls in FileExporter.implementations().items()]
                # plus API
                + [{
                    'url': reverse(f'api-{MOONSHEEP["APP"]}:api-root'),
                    'label': 'Open API'
                }]
        })

        return context


class ExporterView(View):
    def get(self, request, *args, **kwargs):
        exporter_cls = FileExporter.implementations().get(kwargs['slug'], None)
        if exporter_cls is None:
            raise Http404(f"Exporter {kwargs['slug']} does not exist")

        app_label = MOONSHEEP["APP"]
        exp: FileExporter = exporter_cls(app_label)
        # TODO frictionless supporting writing to existing writer/opened file
        temp_dir = tempfile.mkdtemp()
        # TODO exporters should have the option to generate a default file name
        temp_file = os.path.join(temp_dir, app_label + ('.xlsx' if kwargs['slug'] == 'xlsx' else '.tar.gz'))
        # TODO frictionless checks file extension. it should operate by default as "save to one file packed"
        # or we should create an option for that
        # Decide how exporters should behave
        exp.export(temp_file)

        return FileResponse(open(temp_file, 'rb'), as_attachment=True)


class ChooseNicknameView(TemplateView):
    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        nickname = request.GET.get('nickname', None)
        if nickname:
            # try to create user with given nickname & login
            user = None
            try:
                user = User.objects.create_pseudonymous(nickname=nickname)
            except IntegrityError:
                context.update({
                    'nickname_taken': True
                })

            if user:
                # Attach user to the current session
                login(request, user)

                # get redirect, if not go to home
                url = request.GET.get('next', '/')
                return redirect(url)

        context.update({
            'proposed_nickname': generate_nickname()
        })

        return self.render_to_response(context)


def unpack_post(post: QueryDict) -> dict:
    """
    Unpack items in POST fields that have multiple occurences.

    It handles:
    - multiple fields without brackets, ie. field
    - multiple fields PHP5 style, ie. field[]
    - objects, ie. obj[field1]=val1 obj[field2]=val2
    - multiple rows of several fields, ie. row[0][field1], row[1][field1]
    - hierarchily nested multiples, ie. row[0][entry_id], row[0][entry_options][]

    Possible TODO, Django does it like this: (we could use Django parsing)
    <input type="hidden" name="acquisition_titles-TOTAL_FORMS" value="3" id="id_acquisition_titles-TOTAL_FORMS" autocomplete="off">
    <input type="hidden" name="acquisition_titles-INITIAL_FORMS" value="0" id="id_acquisition_titles-INITIAL_FORMS">
    <input type="hidden" name="acquisition_titles-MIN_NUM_FORMS" value="0" id="id_acquisition_titles-MIN_NUM_FORMS">
    <input type="hidden" name="acquisition_titles-MAX_NUM_FORMS" value="1000" id="id_acquisition_titles-MAX_NUM_FORMS" autocomplete="off">
    <input type="hidden" name="acquisition_titles-1-id" id="id_acquisition_titles-1-id">
    <input type="hidden" name="acquisition_titles-1-property" id="id_acquisition_titles-1-property">

    :param QueryDict post: POST data
    :return: dictionary representing the object passed in POST
    """

    dpath_separator = '/'
    result = {}
    convert_to_array_paths = set()

    for k in post.keys():
        # analyze field name
        m = re.search(r"^" +
                      "(?P<object>[\w\-_]+)" +
                      "(?P<selectors>(\[[\d\w\-_]+\])*)" +
                      "(?P<trailing_brackets>\[\])?" +
                      "$", k)
        if not m:
            raise Exception("Field name not valid: {}".format(k))

        path = m.group('object')
        if m.group('selectors'):
            for ms in re.finditer(r'\[([\d\w\-_]+)\]', m.group('selectors')):
                # if it is integer then make sure list is created
                idx = ms.group(1)
                if re.match(r'\d+', idx):
                    convert_to_array_paths.add(path)

                path += dpath_separator + idx

        def get_list_or_value(post, key):
            val = post.getlist(key)
            # single element leave single unless developer put brackets
            if len(val) == 1 and not m.group('trailing_brackets'):
                val = val[0]
            return val

        dpath.util.new(result, path, get_list_or_value(post, k), separator=dpath_separator)

    # dpath only works on dicts, but sometimes we want arrays
    # ie. row[0][fld]=0&row[1][fld]=1 results in row { "0": {}, "1": {} } instead of row [ {}, {} ]
    for path_to_d in convert_to_array_paths:
        arr = []
        d = dpath.util.get(result, path_to_d)
        numeric_keys = [int(k_int) for k_int in d.keys()]
        for k_int in sorted(numeric_keys):
            arr.append(d[str(k_int)])

        dpath.util.set(result, path_to_d, arr)

    return result
