import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sysconfig
import venv
from unittest import mock

from tests.test_stage4_launcher import launcher
from literature_evidence_mcp import mcp_selfcheck

ROOT = Path(__file__).resolve().parents[1]


class IsolatedEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'local-artifacts/formal-ui',prefix='isolated-entry-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.project=self.root/'project with spaces'
        (self.project/'scripts').mkdir(parents=True)
        (self.project/'pyproject.toml').write_text('# synthetic project boundary')
        shutil.copy2(ROOT/'scripts/macos_launcher.py',self.project/'scripts/macos_launcher.py')

    def test_default_is_unchanged_and_invalid_test_names_paths_are_rejected(self):
        home=self.root/'synthetic home'
        default=launcher.resolve_paths(self.project,home=home)
        self.assertEqual(default.application_root,home/'Library/Application Support/literature-evidence-mcp')
        self.assertEqual(default.venv,self.project/'.venv')
        for name in ('../outside','/tmp/escape','bad name','', 'a'*41):
            with self.assertRaises(launcher.LauncherError): launcher.resolve_paths(self.project,test_instance=name)
        external=self.root/'outside';external.mkdir()
        (self.project/'local-artifacts').symlink_to(external,target_is_directory=True)
        with self.assertRaises(launcher.LauncherError): launcher.resolve_paths(self.project,test_instance='check')
        self.assertEqual(list(external.iterdir()),[])

    def test_space_paths_real_guide_root_binding_and_default_template(self):
        paths=launcher.resolve_paths(self.project,test_instance='check')
        launcher.ensure_application_root(paths.application_root)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(paths.venv)
        source=Path(sysconfig.get_path('purelib'))
        destination=paths.venv/'lib'/source.parent.name/'site-packages'
        shutil.copytree(source,destination,dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__editable__*','__pycache__','_virtualenv*'))
        shutil.copytree(ROOT/'src/literature_evidence_mcp',destination/'literature_evidence_mcp',dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__'))
        launcher.install_mcp_shim(paths)
        script='import json,sys; from pathlib import Path; from literature_evidence_mcp.mcp_selfcheck import local_mcp_guide; print(json.dumps(local_mcp_guide(Path(sys.argv[1]))))'
        def guide():
            return json.loads(subprocess.check_output([str(paths.venv_python),'-I','-B','-c',script,str(paths.application_root)],text=True))
        result=guide()
        self.assertEqual(result['state'],'copy_ready_not_configured')
        self.assertEqual(result['instance_kind'],'isolated_test')
        self.assertIn('test-check-',result['server_name'])
        self.assertIn('project with spaces',result['cli'])
        import shlex, tomllib
        cli=shlex.split(result['cli']); config=tomllib.loads(result['toml'])['mcp_servers'][result['server_name']]
        self.assertEqual(cli[-2:],config['args'])
        self.assertEqual(shlex.split(config['args'][1]),['exec',str(paths.mcp_shim)])
        before=paths.mcp_shim.read_text()
        paths.mcp_shim.write_text(before.replace(str(paths.application_root),str(self.root/'outside')))
        self.assertEqual(guide()['state'],'unavailable')
        # The unmodified standard installation still emits the exact HOME-based template.
        paths.mcp_shim.write_text(before)
        standard=launcher.resolve_paths(self.project,home=self.root/'home')
        launcher.ensure_application_root(standard.application_root)
        standard=standard._replace(venv=paths.venv,venv_python=paths.venv_python)
        launcher.install_mcp_shim(standard)
        with mock.patch.object(mcp_selfcheck,'default_application_root',return_value=standard.application_root):
            original=mcp_selfcheck.local_mcp_guide(standard.application_root)
        self.assertEqual(original['server_name'],'literature-evidence')
        self.assertIn('$HOME/Library/Application Support/literature-evidence-mcp/mcp-server',original['cli'])
        self.assertEqual(original['state'],'copy_ready_not_configured')
