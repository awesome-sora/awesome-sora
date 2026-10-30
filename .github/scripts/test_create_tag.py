"""Offline contracts for the actual tag helper; all GitHub transport is synthetic."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch
import urllib.request

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("create_tag", HERE / "create_tag.py")


def load_helper():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def fixture():
    user = {"login": "kubbot", "id": 124379614, "type": "User"}
    event = {
        "action": "created", "sender": user.copy(),
        "repository": {"id": 762617074, "full_name": "awesome-sora/awesome-sora",
                       "default_branch": "main", "archived": False, "fork": False},
        "issue": {"number": 5, "pull_request": {"head": "untrusted-ignored"}},
        "comment": {"id": 99, "user": user.copy(),
                    "body": '/create tag v1.2.3 "Release 中文 $(not-a-command)"'},
    }
    context = {"event_name": "issue_comment", "ref": "refs/heads/main",
               "sha": "a" * 40, "repository": "awesome-sora/awesome-sora",
               "actor": "kubbot", "triggering_actor": "kubbot", "run_attempt": "1"}
    return event, context


class Backend:
    """Only the final HTTP boundary is replaced, not helper policy or orchestration."""
    def __init__(self, module):
        self.module = module
        self.calls = []
        self.permission = {"permission": "write", "role_name": "maintain",
                           "user": {"login": "kubbot", "id": 124379614}}
        self.main_sha = "a" * 40
        self.existing = False
        self.created = False
        self.message = None
        self.fail = None
        self.bad_tag = False
        self.bad_readback = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.fail and self.fail[0:2] == (method, path):
            raise self.module.APIError(self.fail[2])
        if path == "/collaborators/kubbot/permission":
            return copy.deepcopy(self.permission)
        if path == "/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main_sha}}
        if method == "GET" and path == "/git/ref/tags/v1.2.3":
            if self.created or self.existing:
                return {"ref": "refs/tags/v1.2.3", "object": {
                    "type": "tag", "sha": ("c" if self.bad_readback else "b") * 40}}
            raise self.module.APIError(404)
        if method == "POST" and path == "/git/tags":
            self.message = payload["message"]
            return {"sha": "b" * 40, "tag": "v1.2.3", "message": self.message,
                    "object": {"type": "commit", "sha": ("c" if self.bad_tag else "a") * 40}}
        if method == "POST" and path == "/git/refs":
            self.created = True
            return {"ref": "refs/tags/v1.2.3", "object": {"type": "tag", "sha": "b" * 40}}
        if method == "GET" and path == "/git/tags/" + "b" * 40:
            return {"sha": "b" * 40, "tag": "v1.2.3", "message": self.message,
                    "object": {"type": "commit", "sha": "a" * 40}}
        raise AssertionError("Unexpected synthetic endpoint")


class TagContracts(unittest.TestCase):
    def setUp(self):
        self.m = load_helper()
        self.event, self.context = fixture()
        self.api = Backend(self.m)
        self.denies = [patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("network forbidden")),
                       patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
                       patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden"))]
        for guard in self.denies:
            guard.start()
            self.addCleanup(guard.stop)

    def run_actual(self):
        return self.m.create_from_comment(self.event, self.context, self.api)

    def assert_no_writes(self):
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_authorized_command_preserves_annotation_as_data_and_exact_main(self):
        result = self.run_actual()
        self.assertEqual(result, "v1.2.3")
        self.assertEqual(self.api.calls, [
            ("GET", "/collaborators/kubbot/permission", None),
            ("GET", "/git/ref/heads/main", None),
            ("GET", "/git/ref/tags/v1.2.3", None),
            ("POST", "/git/tags", {"tag": "v1.2.3", "message": "Release 中文 $(not-a-command)",
                                    "object": "a" * 40, "type": "commit"}),
            ("POST", "/git/refs", {"ref": "refs/tags/v1.2.3", "sha": "b" * 40}),
            ("GET", "/git/ref/tags/v1.2.3", None),
            ("GET", "/git/tags/" + "b" * 40, None),
        ])

    def test_invalid_shell_or_multiline_command_is_rejected_before_any_api(self):
        bodies = ['/create tag v1.2.3 "x"; printf injected', '/create tag v1.2.3 "x"\nextra',
                  '/create tag v1.2.3 "x\rbody"', '/create tag v1.2.3 "x\x00body"',
                  '/create tag v01.2.3 "x"', '/create tag -f "x"', '/create tag v1.2.3/evil "x"',
                  '/create tag v1.2.3 ""', '/create tag v1.2.3 "   "',
                  '/create tag v1.2.3 "' + 'x' * 513 + '"', '/create tag v1.2.3 x',
                  '/create tag v1.2.3 "x" trailing', '/create tag v1.2.3 "x\x1bbody"']
        for body in bodies:
            with self.subTest(bodyHash=hash(body)):
                self.event["comment"]["body"] = body
                with self.assertRaises(self.m.Refused): self.run_actual()
                self.assertEqual(self.api.calls, [])

    def test_actor_repository_event_and_ref_unknowns_are_rejected_before_api(self):
        changes = [("context", "event_name", "pull_request_review_comment"),
                   ("context", "ref", "refs/pull/5/merge"), ("context", "sha", "main"),
                   ("context", "repository", "awesome-sora/awesome-sora-zh"),
                   ("context", "actor", "attacker"), ("context", "triggering_actor", "attacker"),
                   ("context", "run_attempt", "2"), ("event", "action", "edited"),
                   ("repository", "id", 1), ("repository", "full_name", "fork/awesome-sora"),
                   ("repository", "default_branch", "other"), ("repository", "archived", True),
                   ("repository", "fork", True), ("sender", "id", True),
                   ("sender", "type", "Bot"), ("sender", "login", "x/evil"),
                   ("commenter", "id", 1), ("commenter", "login", "attacker")]
        for group, name, value in changes:
            with self.subTest(group=group, name=name):
                self.event, self.context = fixture()
                target = {"context": self.context, "event": self.event,
                          "repository": self.event["repository"], "sender": self.event["sender"],
                          "commenter": self.event["comment"]["user"]}[group]
                target[name] = value
                with self.assertRaises(self.m.Refused): self.run_actual()
                self.assertEqual(self.api.calls, [])

    def test_missing_or_wrong_payload_shapes_are_rejected(self):
        for path in [("sender",), ("repository",), ("comment",), ("issue",),
                     ("repository", "archived"), ("comment", "user")]:
            with self.subTest(path=path):
                self.event, self.context = fixture()
                obj = self.event
                for key in path[:-1]: obj = obj[key]
                del obj[path[-1]]
                with self.assertRaises(self.m.Refused): self.run_actual()
                self.assertEqual(self.api.calls, [])
        for value in [None, [], "not-object"]:
            with self.assertRaises(self.m.Refused): self.m.create_from_comment(value, self.context, self.api)

    def test_permissions_and_current_user_identity_fail_closed(self):
        for permission in ["read", "none", "triage", "custom", True, None]:
            with self.subTest(permission=permission):
                self.api.permission["permission"] = permission
                with self.assertRaises(self.m.Refused): self.run_actual()
                self.assert_no_writes()
        self.api.permission["permission"] = "write"
        self.api.permission["user"]["id"] = 999
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assert_no_writes()

    def test_permission_403_or_missing_main_never_becomes_clear(self):
        for method, path, status in [("GET", "/collaborators/kubbot/permission", 403),
                                     ("GET", "/collaborators/kubbot/permission", 404),
                                     ("GET", "/git/ref/heads/main", 404),
                                     ("GET", "/git/ref/tags/v1.2.3", 500)]:
            self.api = Backend(self.m); self.api.fail = (method, path, status)
            with self.assertRaises(self.m.APIError): self.run_actual()
            self.assert_no_writes()

    def test_main_moved_or_tag_exists_refuses_without_mutation(self):
        self.api.main_sha = "c" * 40
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assert_no_writes()
        self.api = Backend(self.m); self.api.existing = True
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assert_no_writes()

    def test_wrong_tag_object_is_not_used_to_create_ref(self):
        self.api.bad_tag = True
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assertNotIn(("POST", "/git/refs"), [(m, p) for m, p, _ in self.api.calls])

    def test_create_ref_race_does_not_update_delete_force_or_retry(self):
        self.api.fail = ("POST", "/git/refs", 422)
        with self.assertRaises(self.m.APIError): self.run_actual()
        self.assertEqual(self.api.calls[-1][0:2], ("POST", "/git/refs"))
        self.assertEqual(sum(m == "POST" and p == "/git/refs" for m, p, _ in self.api.calls), 1)

    def test_readback_failure_does_not_rollback_any_ref(self):
        self.api.bad_readback = True
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assertTrue(self.api.created)
        self.assertTrue(all(m in ("GET", "POST") for m, _, _ in self.api.calls))

    def test_cancellation_propagates(self):
        self.api.request = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.run_actual()

    def test_redirects_refuse_before_following(self):
        with self.assertRaises(self.m.Refused):
            self.m.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")

    def test_only_canonical_https_transport_and_no_mutation_verbs(self):
        client = self.m.GitHub("synthetic-never-used")
        for method, path in [("PATCH", "/git/refs/tags/x"), ("DELETE", "/git/refs/tags/x"),
                             ("POST", "https://other.invalid"), ("POST", "/outside")]:
            with self.assertRaises(self.m.Refused): client.request(method, path)

    def test_boolean_commenter_id_is_not_an_integer_identity(self):
        self.event["sender"]["id"] = 1
        self.event["comment"]["user"]["id"] = True
        with self.assertRaises(self.m.Refused): self.run_actual()
        self.assertEqual(self.api.calls, [])

    def test_transport_encodes_json_to_only_canonical_https_with_synthetic_token(self):
        client = self.m.GitHub("synthetic-never-used")
        captured = []
        class LocalOpener:
            def open(self, request, timeout):
                captured.append((request, timeout))
                return io.BytesIO(b'{"ok": true}')
        client._opener = LocalOpener()
        self.assertEqual(client.request("POST", "/git/tags", {"message": "$(literal) 中文"}), {"ok": True})
        request, timeout = captured[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/awesome-sora/awesome-sora/git/tags")
        self.assertEqual(json.loads(request.data), {"message": "$(literal) 中文"})
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-never-used")
        self.assertTrue(0 < timeout <= 8)
        for method, path in [("GET", "/git/trees/main"), ("POST", "/git/refs/heads/main"),
                             ("GET", "/collaborators/a/permission?token=x")]:
            with self.assertRaises(self.m.Refused): client.request(method, path)
        self.assertEqual(len(captured), 1)

    def test_main_suppresses_unknown_transport_details_and_never_retries(self):
        env = {"GITHUB_" + key.upper(): value for key, value in self.context.items()}
        env.update(GITHUB_EVENT_PATH="synthetic-event.json", GITHUB_TOKEN="synthetic-never-used")
        self.api.request = lambda *args: (_ for _ in ()).throw(RuntimeError("SENSITIVE_SENTINEL"))
        output = io.StringIO()
        with patch.dict(self.m.os.environ, env, clear=True), \
                patch("builtins.open", return_value=io.BytesIO(json.dumps(self.event).encode())), \
                patch.object(self.m, "GitHub", return_value=self.api), patch("sys.stderr", output):
            self.assertEqual(self.m.main(), 1)
        self.assertNotIn("SENSITIVE_SENTINEL", output.getvalue())
        self.assertIn("Inspect repository metadata", output.getvalue())

    def test_workflow_uses_trusted_main_script_and_no_comment_shell(self):
        text = (HERE.parent / "workflows/auto-tag.yml").read_text()
        self.assertNotIn("pull_request_review_comment", text)
        self.assertNotIn("github.event.comment.body", text.split("steps:", 1)[1])
        self.assertIn("ref: ${{ github.sha }}", text)
        self.assertIn("persist-credentials: false", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("source $GITHUB_STATE", text)

    def test_pr_contract_job_is_read_only_without_release_trigger(self):
        text = (HERE.parent / "workflows/tag-safety.yml").read_text()
        self.assertIn("contents: read", text)
        self.assertIn("pull_request:", text)
        for forbidden in ["workflow_dispatch", "pull_request_target", "secrets.", "packages:", "push:"]:
            self.assertNotIn(forbidden, text)
        self.assertIn("test_create_tag.py", text)


if __name__ == "__main__":
    unittest.main()
