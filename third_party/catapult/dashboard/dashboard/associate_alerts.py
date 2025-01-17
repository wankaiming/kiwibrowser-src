# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Provides an endpoint and web interface for associating alerts with bug."""

import re

from google.appengine.api import users
from google.appengine.ext import ndb

from dashboard import oauth2_decorator
from dashboard.common import request_handler
from dashboard.common import utils
from dashboard.models import anomaly
from dashboard.services import issue_tracker_service


class AssociateAlertsHandler(request_handler.RequestHandler):
  """Associates alerts with a bug."""

  def post(self):
    """POST is the same as GET for this endpoint."""
    self.get()

  @oauth2_decorator.DECORATOR.oauth_required
  def get(self):
    """Response handler for the page used to group an alert with a bug.

    Request parameters:
      bug_id: Bug ID number, as a string (when submitting the form).
      keys: Comma-separated alert keys in urlsafe format.
      confirm: If non-empty, associate alerts with a bug ID even if
          it appears that the alerts already associated with that bug
          have a non-overlapping revision range.

    Outputs:
      HTML with result.
    """
    if not utils.IsValidSheriffUser():
      user = users.get_current_user()
      self.ReportError('User "%s" not authorized.' % user, status=403)
      return

    urlsafe_keys = self.request.get('keys')
    if not urlsafe_keys:
      self.RenderHtml('bug_result.html', {
          'error': 'No alerts specified to add bugs to.'})
      return

    is_confirmed = bool(self.request.get('confirm'))
    bug_id = self.request.get('bug_id')
    if bug_id:
      self._AssociateAlertsWithBug(bug_id, urlsafe_keys, is_confirmed)
    else:
      self._ShowCommentDialog(urlsafe_keys)

  def _ShowCommentDialog(self, urlsafe_keys):
    """Sends a HTML page with a form for selecting a bug number.

    Args:
      urlsafe_keys: Comma-separated Alert keys in urlsafe format.
    """
    # Get information about Alert entities and related TestMetadata entities,
    # so that they can be compared with recent bugs.
    alert_keys = [ndb.Key(urlsafe=k) for k in urlsafe_keys.split(',')]
    alert_entities = ndb.get_multi(alert_keys)
    ranges = [(a.start_revision, a.end_revision) for a in alert_entities]

    # Mark bugs that have overlapping revision ranges as potentially relevant.
    # On the alerts page, alerts are only highlighted if the revision range
    # overlaps with the revision ranges for all of the selected alerts; the
    # same thing is done here.
    bugs = self._FetchBugs()
    for bug in bugs:
      this_range = _RevisionRangeFromSummary(bug['summary'])
      bug['relevant'] = all(_RangesOverlap(this_range, r) for r in ranges)

    self.RenderHtml('bug_result.html', {
        'bug_associate_form': True,
        'keys': urlsafe_keys,
        'bugs': bugs
    })

  def _FetchBugs(self):
    http = oauth2_decorator.DECORATOR.http()
    issue_tracker = issue_tracker_service.IssueTrackerService(http)
    response = issue_tracker.List(
        q='opened-after:today-5', label='Type-Bug-Regression,Performance',
        sort='-id')
    return response.get('items', []) if response else []

  def _AssociateAlertsWithBug(self, bug_id, urlsafe_keys, is_confirmed):
    """Sets the bug ID for a set of alerts.

    This is done after the user enters and submits a bug ID.

    Args:
      bug_id: Bug ID number, as a string.
      urlsafe_keys: Comma-separated Alert keys in urlsafe format.
      is_confirmed: Whether the user has confirmed that they really want
          to associate the alerts with a bug even if it appears that the
          revision ranges don't overlap.
    """
    # Validate bug ID.
    try:
      bug_id = int(bug_id)
    except ValueError:
      self.RenderHtml(
          'bug_result.html',
          {'error': 'Invalid bug ID "%s".' % str(bug_id)})
      return

    # Get Anomaly entities and related TestMetadata entities.
    alert_keys = [ndb.Key(urlsafe=k) for k in urlsafe_keys.split(',')]
    alert_entities = ndb.get_multi(alert_keys)

    if not is_confirmed:
      warning_msg = self._VerifyAnomaliesOverlap(alert_entities, bug_id)
      if warning_msg:
        self._ShowConfirmDialog('associate_alerts', warning_msg, {
            'bug_id': bug_id,
            'keys': urlsafe_keys,
        })
        return

    for a in alert_entities:
      a.bug_id = bug_id

    ndb.put_multi(alert_entities)

    self.RenderHtml('bug_result.html', {'bug_id': bug_id})

  def _VerifyAnomaliesOverlap(self, alerts, bug_id):
    """Checks whether the alerts' revision ranges intersect.

    Args:
      alerts: A list of Alert entities to verify.
      bug_id: Bug ID number.

    Returns:
      A string with warning message, or None if there's no warning.
    """
    if not utils.MinimumAlertRange(alerts):
      return 'Selected alerts do not have overlapping revision range.'
    else:
      alerts_with_bug = anomaly.Anomaly.query(
          anomaly.Anomaly.bug_id == bug_id).fetch()

      if not alerts_with_bug:
        return None
      if not utils.MinimumAlertRange(alerts_with_bug):
        return ('Alerts in bug %s do not have overlapping revision '
                'range.' % bug_id)
      elif not utils.MinimumAlertRange(alerts + alerts_with_bug):
        return ('Selected alerts do not have overlapping revision '
                'range with alerts in bug %s.' % bug_id)
    return None

  def _ShowConfirmDialog(self, handler, message, parameters):
    """Sends a HTML page with a form to confirm an action.

    Args:
      handler: Name of URL handler to submit confirm dialog.
      message: Confirmation message.
      parameters: Dictionary of request parameters to submit with confirm
                  dialog.
    """
    self.RenderHtml('bug_result.html', {
        'confirmation_required': True,
        'handler': handler,
        'message': message,
        'parameters': parameters or {}
    })


def _RevisionRangeFromSummary(summary):
  """Uses regex to extract revision range from bug a summary string.

  Note: Information such as test path and revision range for a bug could
  also be gotten by querying the datastore for Anomaly entities for
  each bug ID. However, these queries might be relatively costly. Also,
  it is acceptable if the information extracted isn't 100% accurate,
  because it is only used to make a list of bugs for convenience.

  Note: The format of the summary is determined by the triage-dialog element.

  Args:
    summary: The bug summary string.

  Returns:
    A pair of revision numbers (start, end), or None.
  """
  match = re.match(r'.* (\d+):(\d+)$', summary)
  if match:
    start, end = match.groups()
    # Since start and end matched '\d+', we know they can be parsed as ints.
    return (int(start), int(end))
  return None


def _RangesOverlap(range1, range2):
  """Checks whether two revision ranges overlap.

  Note, sharing an endpoint is considered overlap for this function.

  Args:
    range1: A pair of integers (start, end).
    range2: Another pair of integers.

  Returns:
    True if there is any overlap, False otherwise.
  """
  if not range1 or not range2:
    return False
  return range1[0] <= range2[1] and range1[1] >= range2[0]
