from pathlib import Path
import json
import subprocess

import pytest

from app.source_provenance import (MANIFEST_NAME, app_source_inventory,
                                   collect_source_provenance, write_build_manifest)


def source(folder):
    (folder/'app').mkdir(parents=True)
    (folder/'app/__init__.py').write_text('')
    (folder/'app/engine.py').write_text('ALGORITHM = 1\n')
    return folder


def git(folder,*args):
    return subprocess.run(['git','-C',str(folder),*args],check=True,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.decode().strip()


def test_archive_without_manifest_never_discovers_parent_git(tmp_path,monkeypatch):
    (tmp_path/'.git').mkdir()
    runtime=source(tmp_path/'nested-runtime')
    def forbidden(*args,**kwargs):
        raise AssertionError('Git must not run without this source root owning .git')
    monkeypatch.setattr(subprocess,'run',forbidden)
    report=collect_source_provenance(runtime)
    assert report['status']=='MANIFEST_MISSING' and report['commit'] is None
    assert report['file_count']==2 and report['git_discovery_used'] is False
    manifest=write_build_manifest(runtime)
    assert manifest['commit'] is None and manifest['source_matches_commit'] is False
    assert manifest['commit_attestation_reason']=='ROOT_HAS_NO_GIT_MARKER'
    report=collect_source_provenance(runtime)
    assert report['manifest_matches_source'] and not report['commit_verified']


def test_committed_manifest_binds_every_actual_file_and_detects_dirty_or_added_source(tmp_path):
    root=source(tmp_path/'source')
    git(root,'init','-q')
    git(root,'add','app')
    git(root,'-c','user.name=Provenance Test','-c','user.email=provenance@example.invalid',
        'commit','-qm','fixture')
    head=git(root,'rev-parse','HEAD')
    manifest=write_build_manifest(root)
    assert manifest['commit']==head and manifest['source_matches_commit']
    matched=collect_source_provenance(root)
    assert matched['commit']==head and matched['commit_verified']
    assert matched['status']=='BUILD_MANIFEST_VERIFIED'
    (root/'app/engine.py').write_text('ALGORITHM = 2\n')
    dirty=collect_source_provenance(root)
    assert dirty['status']=='MANIFEST_SOURCE_MISMATCH' and dirty['commit'] is None
    assert dirty['app_source_hash']!=matched['app_source_hash']
    current=write_build_manifest(root,overwrite=True)
    assert current['commit'] is None and not current['source_matches_commit']
    dirty=collect_source_provenance(root)
    assert dirty['manifest_matches_source'] and dirty['commit'] is None
    (root/'app/extra.py').write_text('EXTRA = True\n')
    assert collect_source_provenance(root)['status']=='MANIFEST_SOURCE_MISMATCH'
    with pytest.raises(FileExistsError):
        write_build_manifest(root)


def test_copied_runtime_reports_commit_only_with_matching_explicit_manifest(tmp_path):
    root=source(tmp_path/'runtime')
    # Build manifests are portable attestations, not parent-repository guesses.
    manifest=write_build_manifest(root)
    manifest.update(commit='a'*40,source_matches_commit=True)
    external=tmp_path/'explicit-manifest.json'
    external.write_text(json.dumps(manifest))
    assert collect_source_provenance(root)['commit'] is None
    report=collect_source_provenance(root,manifest_path=external)
    assert report['commit']=='a'*40 and report['commit_source']=='EXPLICIT_BUILD_MANIFEST'
    del manifest['app_file_hashes']['app/engine.py']
    external.write_text(json.dumps(manifest))
    report=collect_source_provenance(root,manifest_path=external)
    assert report['status']=='MANIFEST_SOURCE_MISMATCH' and report['commit'] is None


def test_inventory_detects_deleted_file_and_ignores_non_source_and_caches(tmp_path):
    root=source(tmp_path/'runtime')
    old=app_source_inventory(root)
    (root/'.env').write_text('SECRET=never read\n')
    (root/'app/__pycache__').mkdir()
    (root/'app/__pycache__/fake.py').write_text('CACHE=1\n')
    assert app_source_inventory(root)==old
    (root/'app/engine.py').unlink()
    assert app_source_inventory(root)['app_source_hash']!=old['app_source_hash']


@pytest.mark.parametrize('content,status',[
    ('{bad','MANIFEST_UNREADABLE'),
    ('[]','MANIFEST_SCHEMA_MISMATCH'),
    ('{"kind":"other","schema_version":1}','MANIFEST_SCHEMA_MISMATCH')])
def test_invalid_manifest_does_not_claim_commit_or_break_worker(tmp_path,content,status):
    root=source(tmp_path/'runtime')
    (root/MANIFEST_NAME).write_text(content)
    report=collect_source_provenance(root)
    assert report['status']==status and report['commit'] is None
    assert report['app_source_hash']


def test_runtime_hash_seed_reports_current_process_without_claiming_a_commit(tmp_path,monkeypatch):
    import sys
    from app.source_provenance import collect_source_provenance
    app=tmp_path/'app';app.mkdir();(app/'sample.py').write_text('value=1\n')
    monkeypatch.setenv('PYTHONHASHSEED','0')
    result=collect_source_provenance(tmp_path)
    assert result['runtime']['python_hash_seed']=='0'
    assert result['runtime']['hash_randomization']==sys.flags.hash_randomization
    assert result['commit'] is None
