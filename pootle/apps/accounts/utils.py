#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) Pootle contributors.
#
# This file is a part of the Pootle project. It is distributed under the GPL3
# or later license. See the LICENSE file for a copy of the license and the
# AUTHORS file for copyright and authorship information.

import functools
import logging
import sys

from django.contrib.auth import get_user_model

from allauth.account.models import EmailAddress
from allauth.account.utils import sync_user_email_addresses

from pootle_store.models import SuggestionStates
from pootle_store.util import FUZZY, UNTRANSLATED


logger = logging.getLogger(__name__)


def get_user_by_email(email):
    """Retrieves auser by its email address.

    First it looks up the `EmailAddress` entries, and as a safety measure
    falls back to looking up the `User` entries (these addresses are
    sync'ed in theory).

    :param email: address of the user to look up.
    :return: `User` instance belonging to `email`, `None` otherwise.
    """
    try:
        return EmailAddress.objects.get(email__iexact=email).user
    except EmailAddress.DoesNotExist:
        try:
            User = get_user_model()
            return User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return None


def write_stdout(start_msg, end_msg="DONE\n", fail_msg="FAILED\n"):

    def class_wrapper(f):

        @functools.wraps(f)
        def method_wrapper(self, *args, **kwargs):
            sys.stdout.write(start_msg % self.__dict__)
            try:
                f(self, *args, **kwargs)
            except Exception as e:
                sys.stdout.write(fail_msg % self.__dict__)
                raise e
            sys.stdout.write(end_msg % self.__dict__)
        return method_wrapper
    return class_wrapper


class UserMerger(object):

    def __init__(self, src_user, target_user):
        """Purges src_user from site reverting any changes that they have made.

        :param src_user: `User` instance to merge from.
        :param target_user: `User` instance to merge to.
        """
        self.src_user = src_user
        self.target_user = target_user

    @write_stdout("Merging user: "
                  "%(src_user)s --> %(target_user)s...\n",
                  "User merged: %(src_user)s --> %(target_user)s \n")
    def merge(self):
        """Merges one user to another.

        The following are fields are updated (model: fields):
        - units: submitted_by, commented_by, reviewed_by
        - submissions: submitter
        - suggestions: user, reviewer
        """
        self.merge_submitted()
        self.merge_commented()
        self.merge_reviewed()
        self.merge_submissions()
        self.merge_suggestions()
        self.merge_reviews()

    @write_stdout(" * Merging units comments: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_commented(self):
        """Merge commented_by attribute on units
        """
        for unit in self.src_user.commented.iterator():
            unit.commented_by = self.target_user
            unit.save()
            logger.debug("Unit commented_by updated: %s" % repr(unit))

    @write_stdout(" * Merging units reviewed: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_reviewed(self):
        """Merge reviewed_by attribute on units
        """
        # Update submitted_by, commented_by and reviewed_by on units
        for unit in self.src_user.reviewed.iterator():
            unit.reviewed_by = self.target_user
            unit.save()
            logger.debug("Unit reviewed_by updated: %s" % repr(unit))

    @write_stdout(" * Merging suggestion reviews: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_reviews(self):
        """Merge reviewer attribute on suggestions
        """
        for suggestion in self.src_user.reviews.iterator():
            suggestion.reviewer = self.target_user
            suggestion.save()
            logger.debug("Suggestion reviewer updated: %s" % suggestion)

    @write_stdout(" * Merging remaining submissions: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_submissions(self):
        """Merge submitter attribute on submissions
        """
        # Update submitter on submissions
        for submission in self.src_user.submission_set.iterator():
            submission.submitter = self.target_user

            # Before we can save we first have to remove existing score_logs
            # for this submission - they will be recreated on save with correct
            # user.
            for score_log in submission.scorelog_set.iterator():
                score_log.delete()
            submission.save()
            logger.debug("Submission submitter updated: %s" % submission)

    @write_stdout(" * Merging units submitted_by: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_submitted(self):
        """Merge submitted_by attribute on units
        """
        for unit in self.src_user.submitted.iterator():
            unit.submitted_by = self.target_user
            unit.save()
            logger.debug("Unit submitted_by updated: %s" % repr(unit))

    @write_stdout(" * Merging suggestions: "
                  "%(src_user)s --> %(target_user)s... ")
    def merge_suggestions(self):
        """Merge user attribute on suggestions
        """
        # Update user and reviewer on suggestions
        for suggestion in self.src_user.suggestions.iterator():
            suggestion.user = self.target_user
            suggestion.save()
            logger.debug("Suggestion user updated: %s" % suggestion)


class UserPurger(object):

    def __init__(self, user):
        """Purges user from site reverting any changes that they have made.

        :param user: `User` to purge.
        """
        self.user = user

    @write_stdout("Purging user: %(user)s... \n", "User purged: %(user)s \n")
    def purge(self):
        """Purges user from site reverting any changes that they have made.

        The following steps are taken:
        - Delete units created by user and without other submissions.
        - Revert units edited by user.
        - Revert reviews made by user.
        - Revert unit comments by user.
        - Revert unit state changes by user.
        - Delete any remaining submissions and suggestions.
        """

        self.remove_units_created()
        self.revert_units_edited()
        self.revert_units_reviewed()
        self.revert_units_commented()
        self.revert_units_state_changed()

        # Delete remaining submissions.
        for submission in self.user.submission_set.iterator():
            submission.delete()
            logger.debug("Submission deleted: %s" % submission)

        # Delete remaining suggestions.
        for suggestion in self.user.suggestions.iterator():
            suggestion.delete()
            logger.debug("Suggestion deleted: %s" % suggestion)

    @write_stdout(" * Removing units created by: %(user)s... ")
    def remove_units_created(self):
        """Remove units created by user that have not had further
        activity.
        """

        # Delete units created by user without submissions by others.
        for unit in self.user.get_units_created().iterator():

            # Find submissions by other users on this unit.
            other_subs = unit.submission_set.exclude(submitter=self.user)

            if not other_subs.exists():
                unit.delete()
                logger.debug("Unit deleted: %s" % repr(unit))

    @write_stdout(" * Reverting unit comments by: %(user)s... ")
    def revert_units_commented(self):
        """Revert comments made by user on units to previous comment or else
        just remove the comment.
        """

        # Revert unit comments where self.user is latest commenter.
        for unit in self.user.commented.iterator():

            # Find comments by other self.users
            comments = unit.get_comments().exclude(submitter=self.user)

            if comments.exists():
                # If there are previous comments by others update the
                # translator_comment, commented_by, and commented_on
                last_comment = comments.latest('pk')
                unit.translator_comment = last_comment.new_value
                unit.commented_by = last_comment.submitter
                unit.commented_on = last_comment.creation_time
                logger.debug("Unit comment reverted: %s" % repr(unit))
            else:
                unit.translator_comment = ""
                unit.commented_by = None
                unit.commented_on = None
                logger.debug("Unit comment removed: %s" % repr(unit))

            # Increment revision
            unit._comment_updated = True
            unit.save()

    @write_stdout(" * Reverting units edited by: %(user)s... ")
    def revert_units_edited(self):
        """Revert unit edits made by a user to previous edit.
        """
        # Revert unit target where user is the last submitter.
        for unit in self.user.submitted.iterator():

            # Find the last submission by different user that updated the
            # unit.target.
            edits = unit.get_edits().exclude(submitter=self.user)

            if edits.exists():
                last_edit = edits.latest("pk")
                unit.target_f = last_edit.new_value
                unit.submitted_by = last_edit.submitter
                unit.submitted_on = last_edit.creation_time
                logger.debug("Unit edit reverted: %s" % repr(unit))
            else:
                # if there is no previous submissions set the target to ""
                # and set the unit.submitted_by to None
                unit.target_f = ""
                unit.submitted_by = None
                unit.submitted_on = unit.creation_time
                logger.debug("Unit edit removed: %s" % repr(unit))

            # Increment revision
            unit._target_updated = True
            unit.save()

    @write_stdout(" * Reverting units reviewed by: %(user)s... ")
    def revert_units_reviewed(self):
        """Revert reviews made by user on suggestions to previous state.
        """

        # Revert reviews by this user.
        for review in self.user.get_suggestion_reviews().iterator():
            suggestion = review.suggestion
            if suggestion.user == self.user:
                # If the suggestion was also created by this user then remove
                # both review and suggestion.
                suggestion.delete()
                logger.debug("Suggestion removed: %s" % (suggestion))
            elif suggestion.reviewer == self.user:
                # If the suggestion is showing as reviewed by the user, then
                # set the suggestion back to pending and update
                # reviewer/review_time.
                suggestion.state = SuggestionStates.PENDING
                suggestion.reviewer = None
                suggestion.review_time = None
                suggestion.save()
                logger.debug("Suggestion reverted: %s" % (suggestion))

            # Remove the review.
            review.delete()

        for unit in self.user.reviewed.iterator():
            reviews = (unit.get_suggestion_reviews()
                           .exclude(submitter=self.user))
            if reviews.exists():
                previous_review = reviews.latest('pk')
                unit.reviewed_by = previous_review.submitter
                unit.reviewed_on = previous_review.creation_time
                logger.debug("Unit reviewed_by reverted: %s"
                             % (repr(unit)))
            else:
                unit.reviewed_by = None
                unit.reviewed_on = None

                # Increment revision
                unit._target_updated = True
                logger.debug("Unit reviewed_by removed: %s"
                             % (repr(unit)))
            unit.save()

    @write_stdout(" * Reverting unit state changes by: %(user)s... ")
    def revert_units_state_changed(self):
        """Revert unit edits made by a user to previous edit.
        """
        for submission in self.user.get_unit_states_changed().iterator():
            unit = submission.unit

            # We have to get latest by pk as on mysql precision is not to
            # microseconds - so creation_time can be ambiguous
            if submission != unit.get_state_changes().latest('pk'):
                # If the unit has been changed more recently we don't need to
                # revert the unit state.
                submission.delete()
                return
            submission.delete()
            other_submissions = (unit.get_state_changes()
                                     .exclude(submitter=self.user))
            if other_submissions.exists():
                new_state = other_submissions.latest('pk').new_value
            else:
                new_state = UNTRANSLATED
            if new_state != unit.state:
                if unit.state == FUZZY:
                    unit.markfuzzy(False)
                elif new_state == FUZZY:
                    unit.markfuzzy(True)
                unit.state = new_state

                # Increment revision
                unit._state_updated = True
                unit.save()
                logger.debug("Unit state reverted: %s"
                             % (repr(unit)))


def verify_user(user):
    """Verify a user account without email confirmation

    If the user has an existing primary allauth.EmailAddress set then this is
    verified.

    Otherwise, an allauth.EmailAddress is created using email set for
    User.email.

    If the user is already verified raises a ValueError

    :param user: `User` to verify
    """
    # already has primary?
    existing_primary = EmailAddress.objects.filter(user=user, primary=True)
    if existing_primary.exists():
        existing_primary = existing_primary.first()
        if not existing_primary.verified:
            existing_primary.verified = True
            existing_primary.save()
            return
        else:
            # already verified
            raise ValueError("User '%s' is already verified" % user.username)
    sync_user_email_addresses(user)
    email_address = (EmailAddress.objects
                     .filter(user=user, email__iexact=user.email)
                     .order_by("primary")).first()
    email_address.verified = True
    email_address.primary = True
    email_address.save()
