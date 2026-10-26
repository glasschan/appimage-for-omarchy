# jobs.py — the --update --async background job runner (ticket #56).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# The parent side validates the target, takes the job's atomic claim (the
# flock on the job log) and double-forks the detached grandchild; the
# grandchild runs the whole update pipeline (commands/update.py's
# run_update) and publishes one job document per phase. Nothing here ever
# prompts: the parent asks the close-the-running-app question before any
# side effect, and the grandchild re-checks and closes silently.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import logging
import os
import select
import sys
import time

from .. import constants, extractor, janitor, jobs
from .envelope import (EXIT_ERROR, EXIT_OK, OperationError, _make_app_json,
                       _print_json, _resolve_target, _running_app_prompt,
                       _updates_state_key, appimage_provider)


_JOB_HANDSHAKE_TIMEOUT_SECONDS = 10


def _cmd_update_async(target: str, keep_both: bool, force: bool,
                      json_output: bool, kill_running: bool = False,
                      assume_yes: bool = False) -> int:
    """Parent side of --update --async: validate the target (local name/id
    resolution only), take the job's atomic claim, refuse when a job is
    running for the app, then spawn the detached grandchild (see
    _spawn_update_job) and report `job-started` immediately. The parent
    never runs the pipeline itself and never resolves the update manager:
    that is run_update's first step inside the grandchild, so a missing
    or broken source surfaces as a terminal `error` job document instead
    of a second, drifting manager resolution here.

    --async is NOT implicitly non-interactive any more: on a TTY the
    close-the-running-app question is asked HERE, in the parent, before
    anything is spawned (the grandchild's stdin is /dev/null, so a job
    that asked would have to publish skipped-running after a
    `job-started` that already told the user the update was under way).
    A yes becomes the grandchild's --kill-running grant."""
    el = _resolve_target(target)
    # fail closed BEFORE any side effect (no sweep, no fork): the id must
    # be able to name a job file, or this path cannot honour its contract
    desktop_id = _job_desktop_id(el)

    # D: the question, still before any side effect — a decline leaves no
    # claimed log, no sweep and no grandchild behind.
    if not kill_running and not force and not keep_both:
        ask = _running_app_prompt(json_output, assume_yes)
        if ask is not None and appimage_provider.is_app_running(el):
            if not ask(el):
                message = (f'{el.file_path} was skipped because the '
                           'application is running; pass --kill-running to '
                           'close it and update, or --force to replace it '
                           'in place')
                if json_output:
                    _print_json({
                        'schema_version': constants.JSON_SCHEMA_VERSION,
                        'result': 'skipped-running',
                        'message': message,
                        'app': _make_app_json(el),
                    })
                else:
                    print(message)
                return EXIT_OK
            # a yes is the whole job's grant, exactly like --kill-running
            kill_running = True

    # janitor sweep (ticket 03), parent-side only — never in the
    # grandchild: stale terminal/interrupted documents age out and orphan
    # update temp dirs are reclaimed before a new job is spawned.
    # Best-effort by contract: sweep() never raises.
    janitor.sweep()

    # The claim comes first, and it is atomic: two starts racing before
    # either job document exists would otherwise both pass the active-job
    # check and both write this log (TOCTOU). open_job_log_fd also owns
    # the only legal truncation of an over-cap log, so a start that turns
    # out to be refused cannot wipe a live job's log.
    try:
        log_fd = jobs.open_job_log_fd(desktop_id)
    except BlockingIOError:
        return _refuse_second_job(desktop_id, json_output)

    try:
        active = jobs.active_job(desktop_id)
        if active is not None:
            return _refuse_second_job(desktop_id, json_output, active)

        try:
            # `_spawn_update_job` is resolved through the cli facade at
            # CALL time: it is the tests' patch seam
            # (mock.patch.object(cli, '_spawn_update_job'), ticket #56).
            from .. import cli
            pid, job_path, log_path = cli._spawn_update_job(
                desktop_id, target, keep_both=keep_both, force=force,
                log_fd=log_fd, kill_running=kill_running)
        except OSError as e:
            raise OperationError(f'could not start the update job: {e}')
    finally:
        # single owner: the grandchild inherited the description (and the
        # flock with it) through the fork, so this copy is done
        os.close(log_fd)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'job-started',
            'desktop_id': desktop_id,
            'job_path': job_path,
            'log_path': log_path,
            'pid': pid,
        })
    else:
        print(f'Update job started for {el.file_path} '
              f'(pid {pid}, desktop id {desktop_id}); '
              f'job document: {job_path}')
    return EXIT_OK


def _job_desktop_id(el) -> str:
    """The app's job key, with the fail-closed precondition attached: an
    id the job store cannot turn into a file name (a legal .desktop
    filename may hold a space or a non-ASCII char) is an OperationError,
    i.e. the contract's error shape — never a ValueError escaping to
    main()'s catch-all as a generic "unexpected error" + traceback. Sync
    --update accepts such an id, so the refusal has to name the reason."""
    desktop_id = _updates_state_key(el)
    if not jobs.is_usable_desktop_id(desktop_id):
        raise OperationError(
            f'{el.file_path} cannot be updated in the background: its '
            f'desktop id {desktop_id!r} is not usable as a job file name '
            f'(allowed: letters, digits, ".", "_" and "-", '
            f'starting with a letter or digit)')
    return desktop_id


def _refuse_second_job(desktop_id: str, json_output: bool,
                       active: dict = None) -> int:
    """The --async refusal, one shape for both gates (the atomic claim
    and the active-document check). Nothing of the running job is
    touched: its document is not rewritten and its log is not opened for
    writing."""
    pid = (active or {}).get('pid')
    if pid:
        message = (f'an update job is already active for {desktop_id} '
                   f'(pid {pid}); refusing to start a second one')
    else:
        message = (f'an update job is already active for {desktop_id}; '
                   f'refusing to start a second one')
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'desktop_id': desktop_id,
            'error': message,
        })
    else:
        print(f'Error: {message}', file=sys.stderr)
    return EXIT_ERROR


def _job_entrypoint(desktop_id: str, target: str, keep_both: bool,
                    force: bool, kill_running: bool = False) -> int:
    """The detached grandchild's body: run the whole update pipeline and
    publish one job document per phase (jobs.JobWriter). Never raises;
    returns a process exit code (the fork site os._exit()s with it).
    In-process callers (tests) use the same entrypoint, so the sync and
    async pipelines cannot drift."""
    writer = jobs.JobWriter(desktop_id)
    writer.start_heartbeat()
    # G: run_update raises instead of returning a result when it fails, so
    # the only way the error document can carry the force-quit record is
    # this sink — filled the moment the app is actually closed.
    sink = {}
    try:
        try:
            # `run_update` is resolved through the cli facade at CALL time:
            # it is the tests' patch seam
            # (mock.patch.object(cli, 'run_update')), and the facade
            # re-export must stay the name the job runner invokes (ticket
            # #56).
            from .. import cli
            result = cli.run_update(target, keep_both=keep_both, force=force,
                                kill_running=kill_running,
                                progress_cb=writer.progress_cb,
                                on_stage=writer.stage,
                                force_quit_sink=sink, claim_owned=False)
        except Exception as e:
            # the traceback lands in the bounded per-job log (stderr is
            # redirected there), never on stdout
            logging.exception('update job for %s failed', desktop_id)
            writer.finish_error(str(e) or type(e).__name__,
                                force_quit=sink.get('force_quit'))
            return EXIT_ERROR
        return _finish_job_document(writer, result)
    finally:
        writer.stop_heartbeat()
        # process-exit backstop for the grandchild: run_update's scoped
        # cleanup removed what the pipeline registered; whatever is left
        # in the registry dies with this process here
        extractor.cleanup_temp_dirs()


def _finish_job_document(writer, result: UpdateResult) -> int:
    """Map a completed run_update to its terminal job document (same
    verdicts, fail closed, as cmd_update's output branches)."""
    if result.status == 'updated':
        writer.finish('updated', message=result.message,
                      new_version=result.el.version,
                      downloaded_bytes=result.downloaded_bytes,
                      force_quit=result.force_quit)
        return EXIT_OK
    if result.status == 'up-to-date':
        # B: belt on top of run_update's ordering — a document that
        # applied nothing must never say the app was closed for it, so
        # `force_quit` is dropped here whatever the result carries
        writer.finish('up-to-date',
                      message=f'{result.el.file_path} is up to date',
                      force_quit=None)
        return EXIT_OK
    if result.status == 'skipped-running':
        writer.finish('skipped-running', message=result.message,
                      force_quit=result.force_quit)
        return EXIT_OK
    writer.finish_error(f'unknown update result status: {result.status!r}')
    return EXIT_ERROR


def _spawn_update_job(desktop_id: str, target: str, keep_both: bool,
                      force: bool, log_fd: int,
                      kill_running: bool = False) -> tuple:
    """Detach the job runner as a grandchild: os.fork() twice (setsid in
    the intermediate, so the grandchild owns no controlling terminal and
    cannot reacquire one), stdio redirected into the size-capped per-job
    log, stdin from /dev/null. No shell, no exec, nothing interpolated —
    the grandchild continues this process image and runs
    _job_entrypoint, then reports its pid back over a pipe so the parent
    can name it in the job-started document. The parent reaps only the
    intermediate; the grandchild is reparented and reaped by init.

    `log_fd` is the caller's already-open (and flock-claimed) job log, not
    something this function opens: the claim has to be held across the
    active-job check and the fork, and only the caller can bracket that.
    Ownership of the fd stays with the caller.

    Returns (grandchild_pid, job_document_path, job_log_path)."""
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    job_path = jobs.job_document_path(desktop_id)
    log_path = jobs.job_log_path(desktop_id)
    read_fd, write_fd = os.pipe()

    sys.stdout.flush()
    sys.stderr.flush()
    intermediate_pid = os.fork()

    if intermediate_pid == 0:
        # --- intermediate child --------------------------------------
        code = 1
        try:
            os.close(read_fd)
            os.setsid()
            grandchild_pid = os.fork()
        except BaseException:
            try:
                logging.exception('could not fork the update job')
            except Exception:
                pass
            os._exit(code)
        if grandchild_pid == 0:
            # --- grandchild: the detached job ------------------------
            grandchild_code = EXIT_ERROR
            try:
                # write_fd only: the intermediate already closed read_fd
                # before forking, so the grandchild's fd table has no
                # read end to close (a double close here would EBADF and
                # kill the job before its stdio is even redirected)
                os.close(write_fd)
                os.dup2(devnull_fd, 0)
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
                os.close(devnull_fd)
                os.close(log_fd)
                os.chdir('/')
                grandchild_code = _job_entrypoint(
                    desktop_id, target, keep_both=keep_both, force=force,
                    kill_running=kill_running)
            except BaseException:
                try:
                    logging.exception('update job crashed')
                except Exception:
                    pass
                grandchild_code = EXIT_ERROR
            finally:
                os._exit(grandchild_code)
        # report the grandchild pid, then leave: the grandchild is
        # reparented and never reaped by this tree
        try:
            os.write(write_fd, str(grandchild_pid).encode('ascii'))
        except OSError:
            pass
        finally:
            os._exit(0)

    # --- parent ------------------------------------------------------
    os.close(write_fd)
    try:
        data = b''
        deadline = time.monotonic() + _JOB_HANDSHAKE_TIMEOUT_SECONDS
        while len(data) < 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(read_fd, 32)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(read_fd)
        os.close(devnull_fd)
    # bounded above by the handshake deadline only via the pipe: the
    # intermediate's post-fork code is close/setsid/fork/write/_exit, so a
    # hang here would require a signal mid-sequence — reap is unblocking-free
    os.waitpid(intermediate_pid, 0)

    try:
        grandchild_pid = int(data.decode('ascii', errors='replace').strip())
    except ValueError:
        grandchild_pid = 0
    if grandchild_pid <= 0:
        raise OperationError('could not start the update job '
                             '(the detached process died before it '
                             'reported its pid)')
    return grandchild_pid, job_path, log_path


