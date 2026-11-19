"""Refusal controls for the controlled fork's release trust boundary."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import native_release as native


class NativeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.meta = {"id": 100, "run_attempt": 2, "repository": {"full_name": native.REPO},
                     "head_repository": {"full_name": native.REPO}, "path": native.WORKFLOW,
                     "event": "push", "head_sha": "a" * 40, "head_branch": "v2.1.0", "workflow_id": 123,
                     "status": "completed", "conclusion": "success"}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in native.JOBS]
        self.artifacts = [{"id": index + 1, "name": name, "digest": "sha256:" + "b" * 64, "expired": False,
                           "workflow_run": {"id": 100, "head_sha": "a" * 40}} for index, name in enumerate(native.ARTIFACTS)]
        self.binding = self.producer()
        self.binding["workflow_blob"] = "c" * 40
        self.record = {"schema_version": 1, "binding": self.binding, "version": "v2.1.0", "is_release": True, "sign": False}
        self.asset = {"id": 42, "name": native.LOG, "size": 100, "state": "uploaded", "uploader": {"id": native.OPERATOR}}
        self.draft = {"id": 7, "draft": True, "target_commitish": "a" * 40, "tag_name": "v2.1.0",
                      "body": native.PREFIX + json.dumps(self.record), "assets": [self.asset]}
        runtime = {"system": "Darwin", "machine": "arm64", "python": "3.13.16", "python_path": "/fixture/python",
                   "python_sha256": "1" * 64, "go": "go version fixture", "go_path": "/fixture/go", "go_sha256": "2" * 64}
        self.receipt = {"schema_version": 1, "policy": "cct-native-release-v1", "binding": self.binding, "draft_id": 7,
                        "status": "success", "runtime": runtime,
                        "runtime_after": copy.deepcopy(runtime),
                        "commands": ["go build -p 1 ./...", "go vet -p 1 ./...", "go test -p 1 ./...", "bash scripts/smoke-artifact.sh <packaged-darwin-arm64>"],
                        "log": {"id": 42, "sha256": "d" * 64}, "archive_sha256": "e" * 64,
                        "artifact": next(a for a in self.binding["artifacts"] if a["name"] == "cct_darwin_arm64")}

    def producer(self):
        return native.validate_producer(self.meta, self.jobs, self.artifacts, 100, 2)

    def check_receipt(self):
        native.validate_receipt(self.receipt, self.binding, 7, self.asset, "d" * 64)

    def test_valid_controls(self):
        self.assertEqual(self.producer()["source_sha"], "a" * 40)
        with patch.object(native, "verify_tag"):
            self.assertEqual(native.validate_draft(self.draft, self.binding), self.record)
        self.assertEqual(native.one_asset(self.draft, native.LOG, True)["id"], 42)
        self.check_receipt()

    def test_attempt_race(self):
        self.meta["run_attempt"] = 3
        with self.assertRaisesRegex(native.Refused, "attempt changed"):
            self.producer()

    def test_fork_producer(self):
        self.meta["head_repository"]["full_name"] = "other/fork"
        with self.assertRaisesRegex(native.Refused, "repository"):
            self.producer()

    def test_workflow_identity(self):
        self.meta["path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(native.Refused, "workflow"):
            self.producer()

    def test_incomplete_producer(self):
        self.meta["status"] = "in_progress"
        with self.assertRaisesRegex(native.Refused, "did not succeed"):
            self.producer()

    def test_failed_required_job(self):
        self.jobs[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(native.Refused, "unsuccessful job"):
            self.producer()

    def test_duplicate_artifact(self):
        self.artifacts[-1] = copy.deepcopy(self.artifacts[0])
        with self.assertRaisesRegex(native.Refused, "artifact inventory"):
            self.producer()

    def test_artifact_source(self):
        self.artifacts[0]["workflow_run"]["head_sha"] = "f" * 40
        with self.assertRaisesRegex(native.Refused, "source mismatch"):
            self.producer()

    def test_expired_artifact(self):
        self.artifacts[0]["expired"] = True
        with self.assertRaisesRegex(native.Refused, "expired artifact"):
            self.producer()

    def test_missing_artifact_digest(self):
        self.artifacts[0]["digest"] = None
        with self.assertRaisesRegex(native.Refused, "server digest"):
            self.producer()

    def test_default_source_move(self):
        pages = [self.meta, {"total_count": len(self.jobs), "jobs": self.jobs},
                 {"total_count": len(self.artifacts), "artifacts": self.artifacts},
                 {"type": "file", "sha": "c" * 40}, {"sha": "f" * 40}]
        with patch.object(native, "api", side_effect=pages):
            with self.assertRaisesRegex(native.Refused, "current default source"):
                native.producer(100, 2)

    def test_published_draft(self):
        self.draft["draft"] = False
        with patch.object(native, "verify_tag"), self.assertRaisesRegex(native.Refused, "published draft"):
            native.validate_draft(self.draft, self.binding)

    def test_draft_binding(self):
        self.record["binding"] = dict(self.binding, attempt=1)
        self.draft["body"] = native.PREFIX + json.dumps(self.record)
        with patch.object(native, "verify_tag"), self.assertRaisesRegex(native.Refused, "producer binding"):
            native.validate_draft(self.draft, self.binding)

    def test_receipt_author(self):
        self.asset["uploader"]["id"] = 41898282
        with self.assertRaisesRegex(native.Refused, "founder custody"):
            native.one_asset(self.draft, native.LOG, True)

    def test_duplicate_evidence(self):
        self.draft["assets"].append(copy.deepcopy(self.asset))
        with self.assertRaisesRegex(native.Refused, "ambiguous native asset"):
            native.one_asset(self.draft, native.LOG, True)

    def test_receipt_binding(self):
        self.receipt["draft_id"] = 8
        with self.assertRaisesRegex(native.Refused, "receipt binding"):
            self.check_receipt()

    def test_incomplete_workload(self):
        self.receipt["commands"].pop()
        with self.assertRaisesRegex(native.Refused, "workload"):
            self.check_receipt()

    def test_runtime_changed(self):
        self.receipt["runtime_after"]["machine"] = "x86_64"
        with self.assertRaisesRegex(native.Refused, "runtime"):
            self.check_receipt()

    def test_log_binding(self):
        self.receipt["log"]["id"] = 43
        with self.assertRaisesRegex(native.Refused, "log custody"):
            self.check_receipt()

    def test_native_artifact_binding(self):
        self.receipt["artifact"] = dict(self.receipt["artifact"], id=800)
        with self.assertRaisesRegex(native.Refused, "artifact identity"):
            self.check_receipt()

    def test_actual_platform_required(self):
        with patch.object(native.platform, "system", return_value="Linux"), patch.object(native.platform, "machine", return_value="arm64"), patch.dict(native.os.environ, {"DREAMWARE_TEST_ACTIVE": "1"}), patch.object(native, "output", return_value="go version fixture"), patch.object(native, "digest", return_value="f" * 64), patch.object(native.shutil, "which", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(native.Refused, "actual Darwin executor"):
                native.native_runtime()

    def test_ci_failure_stops_workload(self):
        calls = []
        def execute(command, **kwargs):
            calls.append(command[1])
            if command[1] == "vet":
                raise subprocess.CalledProcessError(1, command)
        with patch.object(native, "native_runtime", return_value={"system": "Darwin"}), patch.object(native, "run", side_effect=execute):
            with self.assertRaises(subprocess.CalledProcessError):
                native.native_ci(io.BytesIO())
        self.assertEqual(calls, ["build", "vet"])

    def test_ci_complete_workload(self):
        with patch.object(native, "native_runtime", return_value={"system": "Darwin"}), patch.object(native, "run") as execute:
            self.assertEqual(native.native_ci(io.BytesIO()), {"system": "Darwin"})
            self.assertEqual([call.args[0] for call in execute.call_args_list],
                             [["go", "build", "-p", "1", "./..."], ["go", "vet", "-p", "1", "./..."], ["go", "test", "-p", "1", "./..."]])

    def test_download_asset_digest(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "receipt"
            asset = dict(self.asset, size=4, digest="sha256:" + "0" * 64)
            def download(_remote, destination, _limit):
                destination.write_bytes(b"test")
            with patch.object(native, "download", side_effect=download):
                with self.assertRaisesRegex(native.Refused, "digest mismatch"):
                    native.download_asset(asset, path)

    def test_artifact_endpoint_accepts_json(self):
        import hashlib
        import zipfile
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            def download(_remote, destination, _limit, *, accept):
                self.assertEqual(accept, "application/vnd.github+json")
                with zipfile.ZipFile(destination, "w") as package:
                    package.writestr("cct_fixture.tar.gz", b"archive bytes")
                artifact["digest"] = "sha256:" + native.digest(destination)
            artifact = {"id":123,"digest":"sha256:" + "a" * 64}
            with patch.object(native,"download",side_effect=download):
                path = native.download_artifact(artifact,directory)
            self.assertEqual(path.read_bytes(),b"archive bytes")

    def verification(self, scratch, *, wrong_digest=False, raced=False, tag_moved=False):
        import hashlib
        self.receipt["archive_sha256"] = "0" * 64 if wrong_digest else hashlib.sha256(b"native archive").hexdigest()
        log = b"native log"
        self.receipt["log"]["sha256"] = hashlib.sha256(log).hexdigest()
        receipt_asset = dict(self.asset, id=41, name=native.RECEIPT)
        self.draft["assets"].append(receipt_asset)
        after = copy.deepcopy(self.draft)
        if raced:
            after["assets"][0]["id"] += 1
        def download_asset(asset, destination):
            destination.write_bytes(log if asset["name"] == native.LOG else json.dumps(self.receipt).encode())
        def download_artifact(artifact, directory):
            archive = directory / (artifact["name"] + ".tar.gz")
            archive.write_bytes(b"native archive")
            return archive
        args = SimpleNamespace(run=100, attempt=2, draft=7, output=Path(scratch) / "qualified", compare=None)
        tag = {"sha": ("f" if tag_moved else "a") * 40}
        with patch.object(native, "producer", return_value=self.binding), patch.object(native, "clean_source", return_value="a" * 40), patch.object(native, "api", side_effect=[self.draft, tag, tag, after, tag, tag]), patch.object(native, "download_asset", side_effect=download_asset), patch.object(native, "download_artifact", side_effect=download_artifact), patch.object(native, "github_outputs") as emit:
            native.verify(args)
            return args, emit.call_args.args[0]

    def test_verified_archive_controls(self):
        with tempfile.TemporaryDirectory() as scratch:
            args, values = self.verification(scratch)
            self.assertEqual(len(list(args.output.glob("*.tar.gz"))), 6)
            self.assertEqual(values, {"version": "v2.1.0", "is_release": True, "sign": False, "draft_id": 7})

    def test_native_digest_gates_assembly(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "assembled archive bytes"):
                self.verification(scratch, wrong_digest=True)

    def test_final_custody_race(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "custody or producer raced"):
                self.verification(scratch, raced=True)

    def test_live_release_tag_binding(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "release tag source changed"):
                self.verification(scratch, tag_moved=True)

    def test_existing_tag_target_commitish_is_unused(self):
        self.draft["target_commitish"] = "Main"
        with patch.object(native, "verify_tag") as verify:
            self.assertEqual(native.validate_draft(self.draft, self.binding), self.record)
            verify.assert_called_once_with(self.binding, self.record)

    def test_dry_run_target_source(self):
        binding = dict(self.binding, event="workflow_dispatch")
        record = dict(self.record, binding=binding, version="v0.0.0-dryrun-aaaaaaa", is_release=False)
        draft = dict(self.draft, tag_name="native-handoff-100-2", target_commitish="Main", body=native.PREFIX + json.dumps(record))
        with self.assertRaisesRegex(native.Refused, "dry-run draft source mismatch"):
            native.validate_draft(draft, binding)

    def package(self, directory, *, link=False, header=b"notMachO"):
        import tarfile
        top = "cct_v2.1.0_darwin_arm64"
        archive = directory / (top + ".tar.gz")
        with tarfile.open(archive, "w:gz") as stream:
            entry = tarfile.TarInfo(top)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            stream.addfile(entry)
            for name in ("cct", "README.md", "safety.md"):
                entry = tarfile.TarInfo(top + "/" + name)
                entry.mode = 0o755 if name == "cct" else 0o644
                data = header if name == "cct" else b"fixture"
                entry.size = len(data)
                if link and name == "README.md":
                    entry.type, entry.linkname, entry.size = tarfile.SYMTYPE, "cct", 0
                    stream.addfile(entry)
                else:
                    stream.addfile(entry, io.BytesIO(data))
        return archive

    def test_packaged_architecture(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(native, "run") as execute:
            archive = self.package(Path(scratch))
            with self.assertRaisesRegex(native.Refused, "arm64 Mach-O"):
                native.packaged_smoke(archive, "v2.1.0", io.BytesIO())
            execute.assert_not_called()

    def test_packaged_link_refusal(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(native, "run") as execute:
            archive = self.package(Path(scratch), link=True, header=b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
            with self.assertRaisesRegex(native.Refused, "link or special"):
                native.packaged_smoke(archive, "v2.1.0", io.BytesIO())
            execute.assert_not_called()

    def test_packaged_smoke_controls(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(native, "run") as execute:
            archive = self.package(Path(scratch), header=b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
            self.assertEqual(native.packaged_smoke(archive, "v2.1.0", io.BytesIO()), native.digest(archive))
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0][-1], "cct_v2.1.0_darwin_arm64/cct")


if __name__ == "__main__":
    unittest.main()
