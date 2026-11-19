# Controlled-fork native qualification and release

This applies to `DreamwareDevelopment/codex-claude-transfer`. GitHub-hosted
macOS is denied by default. Linux and Windows CI remain automatic. Native Mac
qualification uses the existing local/fleet test queue, without registering a
Mac as an Actions runner.

## Native CI

From the exact clean committed source, run:

```bash
dreamware-test run --slots 1 -- dreamware-python scripts/native_release.py ci --log /tmp/cct-native-ci.log
```

This requires actual Darwin arm64 and runs Go build, vet and the full unit suite
separately from release smoke. Go, Python, the shared queue and existing test
dependencies must already be installed. The command records actual runtime
binary paths/hashes and refuses a changed source or runtime. It neither sets
`CI`/`GITHUB_*` nor claims hosted CI. Existing Linux encryption coverage remains
required; installing or rotating credentials is not part of this command.

## Release sequence

1. A version tag or manual dry run runs `Release`. It retains every existing
   cross-compiled architecture, the offline dependency proof, archive layout
   checks, and Linux/Windows packaged smoke. It creates a **draft** with a
   bounded source/run/attempt/artifact binding. It does not assemble, sign or
   publish before native proof.
2. The authenticated founder operates the native handoff from a clean checkout
   at the producer's exact source, which must still be current `Main`:

   ```bash
   dreamware-test run --slots 1 -- dreamware-python scripts/native_release.py native --run RUN_ID --attempt ATTEMPT --draft DRAFT_ID --output /tmp/cct-native-RUN_ID-ATTEMPT
   ```

   `RUN_ID`, `ATTEMPT` and `DRAFT_ID` are the actual identities reported by
   GitHub, not approval references. The helper reads server metadata, downloads
   the exact original Darwin arm64 Actions artifact by ID, checks its server
   digest, runs native Go build/vet/test, safely unpacks the same archive and
   runs the existing `scripts/smoke-artifact.sh`. That fixture exercises fake
   homes, cwd/symlink handling, export/import/undo and permissions. The archive
   is never rebuilt after smoke.
3. The helper rechecks source/runtime/producer identities and uploads a log and
   bounded success receipt through normal draft-release asset APIs, without
   clobber. This is an **authenticated founder operator submission**, not a
   cryptographic OS attestation. GitHub's server-observed uploader ID must be
   `25471746` (`DreamwareDevelopment`). Caller-supplied OS JSON, another
   maintainer, and an Actions bot cannot replace that submission.
4. Add `--dispatch` to the native command only when the completed handoff should
   start the `Release` continuation on `Main`. For a real version tag, that
   continuation can publish the release. For a manual producer, it remains a
   dry run, with signing still opt-in. The continuation independently re-fetches
   proof, source, workflow and current attempt; downloads exact original
   artifacts; checks the smoke digest against the assembled Darwin bytes; and
   preserves SBOM, checksums, Sigstore/OIDC signing, attestation and release file
   gates on Linux. It repeats custody and archive checks before publication.

Missing/expired/ambiguous artifacts, a pending/failed or changed attempt, moved
`Main`, wrong uploader/source/workflow, changed bytes, incomplete native work or
an unavailable Mac all leave assembly/publication blocked. Do not rerun the
old hosted-Mac workflow as recovery. Direct administrator/API bypass is outside
this supported-path control.

Drafts and original logs retain the producer evidence. A dry run uses a unique
`native-handoff-RUN_ID-ATTEMPT` draft and never publishes it. No real release,
public signing record or hosted Mac canary is needed to qualify this rollout.

## Helper checks

```bash
dreamware-test run --slots 1 -- dreamware-python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

The native refusal controls are also assertion-mutation checked on disposable
copies. A receipt accounts for the helper's workload and authenticated custody;
it does not turn a local execution into hosted CI or remove Kody/merge gates.
