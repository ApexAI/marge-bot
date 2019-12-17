# pylint: disable=too-many-locals,too-many-branches,too-many-statements
import logging as log
import time
import re
from datetime import datetime

from . import git, gitlab
from .commit import Commit
from .job import CannotMerge, MergeJob, SkipMerge


class SingleMergeJob(MergeJob):

    def __init__(self, *, api, user, project, repo, options, merge_request):
        super().__init__(api=api, user=user, project=project, repo=repo, options=options, merge_request=merge_request)

    def execute(self):
        merge_request = self._merge_request

        log.info('Processing !%s - %r', merge_request.iid, merge_request.title)

        try:
            self.update_merge_request_and_accept()
            log.info('Successfully merged !%s.', merge_request.info['iid'])
        except SkipMerge as err:
            log.warning("Skipping MR !%s: %s", merge_request.info['iid'], err.reason)
        except CannotMerge as err:
            message = "I couldn't merge this branch: %s" % err.reason
            log.warning(message)
            self.unassign_from_mr(merge_request)
            merge_request.comment(message)
        except git.GitError:
            log.exception('Unexpected Git error')
            merge_request.comment('Something seems broken on my local git repo; check my logs!')
            raise
        except Exception:
            log.exception('Unexpected Exception')
            merge_request.comment("I'm broken on the inside, please somebody fix me... :cry:")
            self.unassign_from_mr(merge_request)
            raise

    def update_merge_request_and_accept(self):
        api = self._api
        merge_request = self._merge_request
        updated_into_up_to_date_target_branch = False

        while not updated_into_up_to_date_target_branch:
            self.ensure_mergeable_mr()
            source_project = self.get_source_project(merge_request)
            target_sha = Commit.last_on_branch(self._project.id, merge_request.target_branch, api).id
            diff_version = merge_request.diff_versions[0]
            # Rebase only when necessary
            if diff_version["base_commit_sha"] != target_sha:
                self.rebase_mr()
            else:
                log.info("Skip rebase because FF is possible. ")
            # Don't wait for pipeline if no CI job exists
            if not merge_request.pipeline:
                log.warning("No pipeline found on MR {}. Are you sure about that?".format(merge_request.iid))
            else:
                if source_project.only_allow_merge_if_pipeline_succeeds:
                    self.wait_for_ci_to_pass(merge_request, merge_request.sha)
                    time.sleep(2)
                else:
                    log.warning("Project allows merge without pipeline. Are you sure about that?")

            self.ensure_mergeable_mr()

            try:
                source_branch = merge_request.source_branch.split("-")
                merge_message = "[{issue_id}] {MR_title}".format(
                    issue_id=source_branch[0],
                    MR_title=merge_request.title)
                merge_request.accept(remove_branch=True, sha=merge_request.sha, merge_message=merge_message)
                merge_request.unassign()
            except gitlab.NotAcceptable as err:
                new_target_sha = Commit.last_on_branch(self._project.id, merge_request.target_branch, api).id
                # target_branch has moved under us since we updated, just try again
                if new_target_sha != target_sha:
                    log.info('Someone was naughty and by-passed marge')
                    merge_request.comment(
                        "My job would be easier if people didn't jump the queue and push directly... *sigh*"
                    )
                    continue
                # otherwise the source branch has been pushed to or something
                # unexpected went wrong in either case, we expect the user to
                # explicitly re-assign to marge (after resolving potential
                # problems)
                raise CannotMerge('Merge request was rejected by GitLab: %r' % err.error_message)
            except gitlab.Unauthorized:
                log.warning('Unauthorized!')
                raise CannotMerge('My user cannot accept merge requests!')
            except gitlab.NotFound as ex:
                log.warning('Not Found!: %s', ex)
                merge_request.refetch_info()
                if merge_request.state == 'merged':
                    # someone must have hit "merge when build succeeds" and we lost the race,
                    # the branch is gone and we got a 404. Anyway, our job here is done.
                    # (see #33)
                    updated_into_up_to_date_target_branch = True
                else:
                    log.warning('For the record, merge request state is %r', merge_request.state)
                    raise
            except gitlab.MethodNotAllowed as ex:
                log.warning('Not Allowed!: %s', ex)
                merge_request.refetch_info()
                if merge_request.work_in_progress:
                    raise CannotMerge(
                        'The request was marked as WIP as I was processing it (maybe a WIP commit?)'
                    )
                elif merge_request.state == 'reopened':
                    raise CannotMerge(
                        'GitLab refused to merge this branch. I suspect that a Push Rule or a git-hook '
                        'is rejecting my commits; maybe my email needs to be white-listed?'
                    )
                elif merge_request.state == 'closed':
                    raise CannotMerge('Someone closed the merge request while I was attempting to merge it.')
                elif merge_request.state == 'merged':
                    # We are not covering any observed behaviour here, but if at this
                    # point the request is merged, our job is done, so no need to complain
                    log.info('Merge request is already merged, someone was faster!')
                    updated_into_up_to_date_target_branch = True
                else:
                    raise CannotMerge(
                        "Gitlab refused to merge this request and I don't know why!" + (
                            " Maybe you have unresolved discussions?"
                            if self._project.only_allow_merge_if_all_discussions_are_resolved else ""
                        )
                    )
            except gitlab.ApiError:
                log.exception('Unanticipated ApiError from GitLab on merge attempt')
                raise CannotMerge('had some issue with GitLab, check my logs...')
            else:
                self.wait_for_branch_to_be_merged()
                updated_into_up_to_date_target_branch = True

    def wait_for_branch_to_be_merged(self):
        merge_request = self._merge_request
        time_0 = datetime.utcnow()
        waiting_time_in_secs = 30

        while datetime.utcnow() - time_0 < self._merge_timeout:
            merge_request.refetch_info()

            if merge_request.state == 'merged':
                # Close the issue if target branch is not master
                if merge_request.target_branch != "master":
                    source_project = self.get_source_project(merge_request)
                    issue_ids = re.findall(r'[C|c]loses?\s#(\d*)', merge_request.description)
                    if issue_ids:
                        for issue in set(issue_ids):
                            gitlab.close_issue(self._api, source_project.id, issue)
                            log.info("Close related issue %s", issue)
                        merge_request.comment("Closing #" + ", #".join(issue_ids) + ".")
                    else:
                        merge_request.comment(
                            "No `Close #` command is found in MR description.")
                return  # success!
            if merge_request.state == 'closed':
                raise CannotMerge('someone closed the merge request while merging!')
            assert merge_request.state in ('opened', 'reopened', 'locked'), merge_request.state

            log.info('Giving %s more secs for !%s to be merged...', waiting_time_in_secs, merge_request.iid)
            time.sleep(waiting_time_in_secs)

        raise CannotMerge('It is taking too long to see the request marked as merged!')
