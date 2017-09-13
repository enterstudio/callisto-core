'''

View partials provide all the callisto-core front-end functionality.
Subclass these partials with your own views if you are implementing
callisto-core. Many of the view partials only provide a subset of the
functionality required for a full HTML view.

docs / reference:
    - https://docs.djangoproject.com/en/1.11/topics/class-based-views/

view_partials should define:
    - forms
    - models
    - helper classes
    - access checks
    - anything else that doesn't belong in views.py or urls.py

and should not define:
    - templates
    - redirect urls

'''
import logging

import ratelimit.mixins
from nacl.exceptions import CryptoError

from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect
from django.views import generic as views

from wizard_builder import views as wizard_builder_views

from . import fields, forms, models, view_helpers
from ..reporting import report_delivery

logger = logging.getLogger(__name__)


#######################
# secret key partials #
#######################


class SecretKeyTemplatePartial(
    views.base.TemplateView,
):
    storage_helper = view_helpers.SecretKeyStorageHelper

    @property
    def storage(self):
        return self.storage_helper(self)


class KeyResetTemplatePartial(
    SecretKeyTemplatePartial,
):

    def dispatch(self, request, *args, **kwargs):
        self.storage.clear_secret_key()
        return super().dispatch(request, *args, **kwargs)


###################
# report partials #
###################


# TODO: generalize all of these to be about Model / Object, rather than Report
# the intent there being more effective use of django builtin functionality


class ReportBasePartial(
    wizard_builder_views.WizardFormPartial,
):
    model = models.Report
    storage_helper = view_helpers.EncryptedReportStorageHelper

    @property
    def site_id(self):
        # TODO: remove
        return get_current_site(self.request).id

    @property
    def decrypted_report(self):
        return self.report.decrypted_report(self.storage.secret_key)

    def get_form_kwargs(self):
        # TODO: remove
        kwargs = super().get_form_kwargs()
        kwargs.update({'view': self})
        return kwargs


class ReportCreatePartial(
    ReportBasePartial,
    views.edit.CreateView,
):
    form_class = forms.ReportCreateForm

    def form_valid(self, form):
        self._set_key_from_form(form)
        return super().form_valid(form)

    def _set_key_from_form(self, form):
        if form.data.get('key'):
            self.storage.set_secret_key(form.data['key'])


class _ReportDetailPartial(
    ReportBasePartial,
    views.detail.DetailView,
):
    context_object_name = 'report'
    slug_field = 'uuid'
    slug_url_kwarg = 'uuid'

    @property
    def report(self):
        # TODO: remove, use self.object
        return self.get_object()


class _ReportLimitedDetailPartial(
    _ReportDetailPartial,
    ratelimit.mixins.RatelimitMixin,
):
    ratelimit_key = 'user'
    ratelimit_rate = settings.DECRYPT_THROTTLE_RATE


class _ReportAccessPartial(
    _ReportLimitedDetailPartial,
):
    valid_access_message = 'Valid access request at {}'
    invalid_access_key_message = 'Invalid (key) access request at {}'
    invalid_access_user_message = 'Invalid (user) access request at {}'
    invalid_access_no_key_message = 'Invalid (no key) access request at {}'
    access_form_class = forms.ReportAccessForm

    @property
    def access_granted(self):
        self._check_report_owner()
        if self.storage.secret_key:
            try:
                self.decrypted_report
                self._log_info(self.valid_access_message)
                return True
            except CryptoError:
                self._log_warn(self.invalid_access_key_message)
                return False
        else:
            self._log_info(self.invalid_access_no_key_message)
            return False

    @property
    def access_form_valid(self):
        form = self._get_access_form()
        if form.is_valid():
            # TODO: dont hardcode passphrase POST arg
            self.storage.set_secret_key(self.request.POST.get('key'))
            return True
        else:
            return False

    @property
    def object_form_valid(self):
        self.object = self.report
        form = self.get_form()
        return form.is_valid()

    @property
    def object_form_has_passphrase(self):
        form = self.get_form()
        for field_name, field_object in form.fields.items():
            if (
                field_name == 'key' and
                isinstance(field_object, fields.PassphraseField)
            ):
                return True

    @property
    def pass_access_through(self):
        return bool(
            self.access_form_valid and
            self.object_form_valid and
            self.object_form_has_passphrase
        )

    def dispatch(self, request, *args, **kwargs):
        if self.storage.secret_key or self.pass_access_through:
            return super().dispatch(request, *args, **kwargs)
        elif self.access_form_valid:
            return HttpResponseRedirect(self.request.path)
        else:
            return self._render_access_form()

    def _get_access_form(self):
        form_kwargs = self.get_form_kwargs()
        form_kwargs.update({'instance': self.get_object()})
        return self.access_form_class(**form_kwargs)

    def _render_access_form(self):
        self.object = self.report
        self.template_name = self.access_template_name
        context = self.get_context_data(form=self._get_access_form())
        return self.render_to_response(context)

    def _check_report_owner(self):
        if settings.CALLISTO_CHECK_REPORT_OWNER:
            if not self.report.owner == self.request.user:
                self._log_warn(self.invalid_access_user_message)
                raise PermissionDenied

    def _log_info(self, msg):
        # TODO: remove
        self._log(msg, logger.info)

    def _log_warn(self, msg):
        # TODO: remove
        self._log(msg, logger.warn)

    def _log(self, msg, log):
        # TODO: remove
        path = self.request.get_full_path()
        log(msg.format(path))


class ReportUpdatePartial(
    _ReportAccessPartial,
    views.edit.UpdateView,
):

    @property
    def report(self):
        # TODO: remove, use self.object
        return self.get_object()


class ReportActionPartial(
    ReportUpdatePartial,
):
    success_url = '/'
    form_class = forms.ReportAccessForm

    def form_valid(self, form):
        output = super().form_valid(form)
        self.view_action()
        self.storage.clear_secret_key()
        return output

    def view_action(self):
        pass


class ReportDeletePartial(
    ReportActionPartial,
):

    def view_action(self):
        self.report.delete()


###################
# wizard partials #
###################


class EncryptedWizardPartial(
    ReportUpdatePartial,
    wizard_builder_views.WizardView,
):
    steps_helper = view_helpers.ReportStepsHelper

    def dispatch(self, request, *args, **kwargs):
        self._dispatch_processing()
        return super().dispatch(request, *args, **kwargs)


class WizardActionPartial(
    EncryptedWizardPartial,
):

    def dispatch(self, request, *args, **kwargs):
        self.kwargs['step'] = view_helpers.ReportStepsHelper.done_name
        return super().dispatch(request, *args, **kwargs)


class WizardPDFPartial(
    WizardActionPartial,
):

    def report_pdf_response(self):
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = 'inline; filename="report.pdf"'
        # TODO: importing from reporting smells bad
        response.write(report_delivery.report_as_pdf(
            report=self.report,
            data=self.storage.cleaned_form_data,
            recipient=None,
        ))
        return response
