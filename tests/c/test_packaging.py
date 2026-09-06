"""A staged source tree must load its own frozen artifact without the old checkout."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from track_c.learning.config import validate, sources
from track_c.ops.deploy import bundle
from track_c.replay.engine import build
from track_c.replay.evidence import read_model
from tests.c.test_learning import synthetic


class PackagingTests(unittest.TestCase):
    def test_release_contains_source_identity_and_tests_but_no_secrets_or_retired_owners(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with redirect_stdout(io.StringIO()):build(synthetic(root),root/'run',validate())
            model=root/'run/model.json'
            doc,_,_=read_model(model)
            body,release,identity=bundle(model)
            self.assertEqual(identity,doc['digest'])
            with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as archive:
                members={member.name:archive.extractfile(member).read() for member in archive.getmembers()}
            self.assertFalse(any(name.startswith(('bot/','quant/','track_c/c4/','track_c/c4_refined/')) for name in members))
            self.assertFalse(any('.env' in name or name.endswith(('.sqlite','.pem','.pyc')) for name in members))
            self.assertIn('tests/c/test_live.py',members)
            self.assertIn('track_c/live.py',members)
            for name,digest in sources().items():
                self.assertEqual(hashlib.sha256(members[name]).hexdigest(),digest,name)
            manifest=json.loads(members['manifest.json'])
            for name,digest in manifest.items():self.assertEqual(hashlib.sha256(members[name]).hexdigest(),digest,name)
