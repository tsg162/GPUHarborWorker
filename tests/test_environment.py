from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from gpuharbor.common.job_spec import TrainingEnvironment
from gpuharbor.worker.environment import prepare_environment, interpreter

class EnvironmentTests(unittest.TestCase):
    def test_explicit_training_interpreter_is_not_replaced_by_worker_python(self):
        with patch.dict(os.environ, {'GPUHARBOR_TRAINING_PYTHON':sys.executable}):
            self.assertEqual(interpreter('python3'),sys.executable)

    def test_hashed_environment_cache_reuses_completed_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir()
            (project/'requirements.lock').write_text('example==1.0 --hash=sha256:'+'a'*64+'\n')
            spec=TrainingEnvironment(python=sys.executable, requirements_lock='requirements.lock')
            def run(command, **kwargs):
                if 'venv' in command:
                    target=Path(command[-1]);(target/'bin').mkdir(parents=True)
                    (target/'bin/python').write_text('placeholder')
            with patch('gpuharbor.worker.environment.subprocess.check_output',return_value='python-identity'),patch('gpuharbor.worker.environment.subprocess.run',side_effect=run) as install:
                first=prepare_environment(spec,project,root/'cache')
                second=prepare_environment(spec,project,root/'cache')
                self.assertEqual(first,second);self.assertEqual(install.call_count,2)
                self.assertIn('--require-hashes',install.call_args.args[0])
                (project/'requirements.lock').write_text('changed==2 --hash=sha256:'+'b'*64+'\n')
                third=prepare_environment(spec,project,root/'cache')
                self.assertNotEqual(first,third);self.assertEqual(install.call_count,4)

    def test_nested_requirement_files_cannot_escape_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'requirements.lock').write_text('-r other.txt\n')
            with self.assertRaisesRegex(ValueError,'flattened'):
                prepare_environment(TrainingEnvironment(python=sys.executable,requirements_lock='requirements.lock'),root,root/'cache')
