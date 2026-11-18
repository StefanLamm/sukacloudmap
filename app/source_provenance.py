"""Exact Python-app source identities for archives, workers and built runtimes.

Runtime code can live below another repository. Never let Git discover a parent
repository and never label modified/archived files with that repository's HEAD.
A commit is an explicit build-manifest attestation; its complete file inventory
must still match the files the worker is actually importing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile


MANIFEST_NAME = 'build-source-manifest.json'
MANIFEST_KIND = 'SUKA_PYTHON_APP_SOURCE_BUILD'
SOURCE_SCOPE = 'ALL_APP_PYTHON_FILES'
SCHEMA_VERSION = 1
_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def _root(value=None):
    return Path(value).resolve() if value is not None else Path(__file__).resolve().parents[1]


def _digest(files):
    raw = json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def app_source_inventory(root=None):
    """Hash actual bytes, including newly added/untracked Python app files.

    No database, environment/configuration value, Git metadata or cache content
    is included. The inventory itself is part of the aggregate hash, so adding
    or deleting a file changes the identity even if other file bytes match.
    """
    folder = _root(root)
    application = folder / 'app'
    if not application.is_dir():
        raise ValueError('APP_SOURCE_DIRECTORY_MISSING')
    files = {}
    for path in sorted(application.rglob('*.py')):
        if '__pycache__' in path.relative_to(application).parts or not path.is_file():
            continue
        files[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError('APP_SOURCE_EMPTY')
    return {'scope': SOURCE_SCOPE, 'file_count': len(files),
            'app_source_hash': _digest(files), 'app_file_hashes': files}


def collect_source_provenance(root=None, *, manifest_path=None):
    """Worker entry: safe to call without a .git directory or build manifest.

    ``commit`` is null unless an explicit manifest attests a clean committed
    build AND its entire app inventory matches the current files. This function
    never invokes Git. In particular it never searches outside ``root``.
    """
    folder = _root(root)
    path = Path(manifest_path).resolve() if manifest_path is not None else folder / MANIFEST_NAME
    result = {'schema_version': SCHEMA_VERSION, 'scope': SOURCE_SCOPE,
              'source_root': str(folder), 'commit': None, 'commit_verified': False,
              'runtime': {'python_version': sys.version.split()[0],
                  'python_hash_seed': os.environ.get('PYTHONHASHSEED'),
                  'hash_randomization': sys.flags.hash_randomization,
                  'native_thread_environment': {key: os.environ.get(key) for key in (
                      'OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS',
                      'VECLIB_MAXIMUM_THREADS','NUMEXPR_NUM_THREADS')}},
              'commit_verification_scope': SOURCE_SCOPE, 'git_discovery_used': False,
              'manifest_path': str(path), 'manifest_matches_source': False}
    try:
        actual = app_source_inventory(folder)
    except (OSError, ValueError):
        return {**result, 'status': 'SOURCE_UNREADABLE', 'app_source_hash': None,
                'app_file_hashes': {}, 'file_count': 0}
    result.update(actual)
    try:
        manifest = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {**result, 'status': 'MANIFEST_MISSING'}
    except (OSError, ValueError, TypeError):
        return {**result, 'status': 'MANIFEST_UNREADABLE'}
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA_VERSION
            or manifest.get('kind') != MANIFEST_KIND or manifest.get('scope') != SOURCE_SCOPE):
        return {**result, 'status': 'MANIFEST_SCHEMA_MISMATCH'}
    if (manifest.get('app_source_hash') != actual['app_source_hash']
            or manifest.get('app_file_hashes') != actual['app_file_hashes']
            or manifest.get('file_count') != actual['file_count']):
        return {**result, 'status': 'MANIFEST_SOURCE_MISMATCH'}
    result['manifest_matches_source'] = True
    commit = manifest.get('commit')
    if (manifest.get('source_matches_commit') is not True or not isinstance(commit, str)
            or not _COMMIT.fullmatch(commit)):
        return {**result, 'status': 'HASH_IDENTIFIED_WITHOUT_COMMIT_ATTESTATION'}
    return {**result, 'status': 'BUILD_MANIFEST_VERIFIED', 'commit': commit,
            'commit_verified': True, 'commit_source': 'EXPLICIT_BUILD_MANIFEST'}


def _local_commit_attestation(folder, actual):
    """Only inspect Git when root itself owns a .git marker (worktrees included)."""
    if not (folder / '.git').exists():
        return None, False, 'ROOT_HAS_NO_GIT_MARKER'

    def git(*arguments):
        return subprocess.run(['git', '-C', str(folder), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5).stdout

    try:
        # Even a supplied .git marker cannot redirect provenance to another
        # working tree. This command is never called without that local marker.
        top = Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip()).resolve()
        if top != folder:
            return None, False, 'GIT_TOPLEVEL_DIFFERS_FROM_SOURCE_ROOT'
        commit = git('rev-parse', 'HEAD').decode('ascii').strip()
        if not _COMMIT.fullmatch(commit):
            return None, False, 'INVALID_GIT_COMMIT'
        archive = git('archive', '--format=tar', commit, 'app')
        committed_files = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
            for member in source.getmembers():
                path = Path(member.name)
                if (not member.isfile() or path.suffix != '.py' or path.parts[0] != 'app'
                        or '__pycache__' in path.parts):
                    continue
                stream = source.extractfile(member)
                if stream is None:
                    return None, False, 'COMMITTED_SOURCE_UNREADABLE'
                committed_files[path.as_posix()] = hashlib.sha256(stream.read()).hexdigest()
        matched = committed_files == actual['app_file_hashes']
        return (commit if matched else None), matched, ('COMMITTED_APP_MATCH' if matched else 'APP_DIFFERS_FROM_COMMIT')
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, tarfile.TarError):
        return None, False, 'LOCAL_GIT_ATTESTATION_FAILED'


def write_build_manifest(root=None, path=None, *, overwrite=False):
    """Build-time helper; emits a hash-only manifest for dirty/archive builds.

    A source commit is recorded only after comparing the complete committed
    app tree against actual bytes. Documentation/non-app files are outside the
    declared scope. No Git config, account, index or working files are changed.
    """
    folder = _root(root)
    target = Path(path).resolve() if path is not None else folder / MANIFEST_NAME
    actual = app_source_inventory(folder)
    commit, matched, reason = _local_commit_attestation(folder, actual)
    value = {'schema_version': SCHEMA_VERSION, 'kind': MANIFEST_KIND,
             'created_at': datetime.now(timezone.utc).isoformat(), **actual,
             'commit': commit, 'source_matches_commit': matched,
             'commit_attestation_reason': reason}
    # The caller chooses the build artifact destination explicitly. Exclusive
    # default avoids overwriting an existing runtime's captured provenance.
    with target.open('w' if overwrite else 'x', encoding='utf-8') as destination:
        json.dump(value, destination, ensure_ascii=True, sort_keys=True, indent=2)
        destination.write('\n')
    return value
