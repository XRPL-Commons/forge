import unittest

from forge.factory import source_inputs as inputs


class SourceInputTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'base_oid': 'a' * 40, 'requirements_revision_id': 'revision-1'}
        self.notes = {'id': 'job-1', 'role': 'build', 'members': [{'body': 'Implement this issue'}]}
        self.source = inputs.create(repository_id=12, canonical_repository='Example/project',
            base_oid='a' * 40, input_oids=['c' * 40, 'b' * 40, 'c' * 40],
            manifest=self.manifest, dependency_policy_digest='d' * 64, notes=self.notes)

    def test_manifest_and_note_changes_cannot_reuse_source_authority(self):
        for notes, manifest in (({**self.notes, 'role': 'audit'}, self.manifest),
                                (self.notes, {**self.manifest, 'base_oid': 'e' * 40})):
            with self.subTest(notes=notes, manifest=manifest), self.assertRaises(ValueError):
                inputs.assignment(self.source, notes, manifest=manifest)

    def test_document_does_not_share_mutable_objects_with_host_record(self):
        note = inputs.assignment(self.source, self.notes, manifest=self.manifest)
        note['members'][0]['body'] = 'Change another repository'
        note['source_input']['input_oids'].clear()
        self.assertEqual(self.notes['members'][0]['body'], 'Implement this issue')
        self.assertEqual(self.source['input_oids'], ['b' * 40, 'c' * 40])

    def test_rejects_ambiguous_identity_and_extra_authority(self):
        for change in ({'repository_id': True}, {'canonical_repository': 'Example/../private'},
                       {'base_oid': 'refs/heads/main'}, {'token': 'forbidden'},
                       {'object_format': 'unknown'}, {'notes_digest': 'mutable'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                inputs.validate({**self.source, **change})

    def test_source_uses_declared_object_format(self):
        with self.assertRaises(ValueError):
            inputs.validate({**self.source, 'object_format': 'sha256'})
        value = {**self.source, 'object_format': 'sha256', 'base_oid': 'a' * 64, 'input_oids': []}
        self.assertEqual(inputs.validate(value), value)

    def test_note_size_is_checked_before_document_publication(self):
        notes = {'text': 'x' * inputs.MAX_NOTES_BYTES}
        with self.assertRaises(ValueError):
            inputs.assignment({**self.source, 'notes_digest': inputs.digest(notes)}, notes, manifest=self.manifest)


if __name__ == '__main__':
    unittest.main()
