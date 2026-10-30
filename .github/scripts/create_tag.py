"""Create one same-repository tag from an authorized, immutable-main comment event."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "awesome-sora/awesome-sora"
REPOSITORY_ID = 762617074
SHA = re.compile(r"[0-9a-f]{40}\Z")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
COMMAND = re.compile(r'/create tag (v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)) "([^"\r\n]+)"\Z')


class Refused(RuntimeError):
    """Unknown or rejected policy, never an authorization fallback."""


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("GitHub request failed")


def require(condition):
    if not condition:
        raise Refused("Tag command refused")


def mapping(value):
    require(isinstance(value, dict))
    return value


def positive_id(value):
    return type(value) is int and value > 0


def parse_command(body):
    require(isinstance(body, str) and len(body.encode("utf-8")) <= 1024)
    match = COMMAND.fullmatch(body)
    require(match is not None)
    tag, message = match.groups()
    require(len(tag) <= 64 and message.strip() and len(message.encode("utf-8")) <= 512)
    require(all(ord(char) >= 32 and ord(char) != 127 for char in message))
    return tag, message


def validate_event(event, context):
    event, context = mapping(event), mapping(context)
    require(context.get("event_name") == "issue_comment" and event.get("action") == "created")
    require(context.get("repository") == REPOSITORY and context.get("ref") == "refs/heads/main")
    require(context.get("run_attempt") == "1")
    sha = context.get("sha")
    require(isinstance(sha, str) and SHA.fullmatch(sha))
    repo = mapping(event.get("repository"))
    require(type(repo.get("id")) is int and repo["id"] == REPOSITORY_ID)
    require(repo.get("full_name") == REPOSITORY and repo.get("default_branch") == "main")
    require(repo.get("archived") is False and repo.get("fork") is False)
    require(positive_id(mapping(event.get("issue")).get("number")))
    comment, sender = mapping(event.get("comment")), mapping(event.get("sender"))
    user = mapping(comment.get("user"))
    login = sender.get("login")
    require(isinstance(login, str) and LOGIN.fullmatch(login))
    require(sender.get("type") == "User" and user.get("type") == "User")
    require(positive_id(sender.get("id")) and positive_id(user.get("id")) and user["id"] == sender["id"])
    require(isinstance(user.get("login"), str) and user["login"].lower() == login.lower())
    for name in ("actor", "triggering_actor"):
        value = context.get(name)
        require(isinstance(value, str) and value.lower() == login.lower())
    require(positive_id(comment.get("id")))
    tag, message = parse_command(comment.get("body"))
    return login, sender["id"], sha, tag, message


def tag_matches(value, sha, tag, message):
    value = mapping(value)
    target = mapping(value.get("object"))
    return (value.get("tag") == tag and value.get("message") == message
            and target.get("type") == "commit" and target.get("sha") == sha)


def ref_matches(value, ref, sha, kind):
    value = mapping(value)
    target = mapping(value.get("object"))
    return value.get("ref") == ref and target.get("type") == kind and target.get("sha") == sha


def create_from_comment(event, context, client):
    login, user_id, sha, tag, message = validate_event(event, context)
    permission = mapping(client.request("GET", "/collaborators/" + urllib.parse.quote(login, safe="") + "/permission"))
    current_user = mapping(permission.get("user"))
    require(permission.get("permission") in ("admin", "maintain", "write"))
    require(type(current_user.get("id")) is int and current_user["id"] == user_id)
    require(isinstance(current_user.get("login"), str) and current_user["login"].lower() == login.lower())
    require(ref_matches(client.request("GET", "/git/ref/heads/main"), "refs/heads/main", sha, "commit"))
    ref_path = "/git/ref/tags/" + tag
    try:
        client.request("GET", ref_path)
    except APIError as error:
        if error.status != 404:
            raise
    else:
        raise Refused("Existing tag is never overwritten")
    # Only create APIs are used. A concurrent existing ref rejects creation; no force/update/delete fallback.
    created = mapping(client.request("POST", "/git/tags", {
        "tag": tag, "message": message, "object": sha, "type": "commit"}))
    require(tag_matches(created, sha, tag, message))
    tag_sha = created.get("sha")
    require(isinstance(tag_sha, str) and SHA.fullmatch(tag_sha))
    ref = "refs/tags/" + tag
    require(ref_matches(client.request("POST", "/git/refs", {"ref": ref, "sha": tag_sha}), ref, tag_sha, "tag"))
    require(ref_matches(client.request("GET", ref_path), ref, tag_sha, "tag"))
    readback = mapping(client.request("GET", "/git/tags/" + tag_sha))
    require(readback.get("sha") == tag_sha and tag_matches(readback, sha, tag, message))
    return tag


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise Refused("Redirect refused")


class GitHub:
    def __init__(self, token):
        require(isinstance(token, str) and token)
        self._token = token
        self._deadline = time.monotonic() + 30
        self._opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        require(method in ("GET", "POST"))
        require(isinstance(path, str))
        if method == "POST":
            require(path in ("/git/tags", "/git/refs"))
        else:
            require(re.fullmatch(
                r"/collaborators/[A-Za-z0-9-]+/permission|/git/ref/heads/main|"
                r"/git/ref/tags/v[0-9]+\.[0-9]+\.[0-9]+|/git/tags/[0-9a-f]{40}", path))
        remaining = self._deadline - time.monotonic()
        require(remaining > 0)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
                                         data=body, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
            "User-Agent": "sora-tag-safety"})
        try:
            with self._opener.open(request, timeout=min(8, remaining)) as response:
                data = response.read(262145)
                require(len(data) <= 262144)
                return json.loads(data)
        except urllib.error.HTTPError as error:
            error.close()
            raise APIError(error.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise Refused("GitHub request unavailable") from None


def main():
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], "rb") as handle:
            raw = handle.read(1048577)
        require(len(raw) <= 1048576)
        event = json.loads(raw)
        names = ("event_name", "ref", "sha", "repository", "actor", "triggering_actor", "run_attempt")
        context = {name: os.environ.get("GITHUB_" + name.upper()) for name in names}
        create_from_comment(event, context, GitHub(os.environ.get("GITHUB_TOKEN")))
    except Exception:
        # A failure may follow tag creation; never automatically retry or delete a remote ref.
        print("::error::Tag command failed or was refused. Inspect repository metadata before retrying.", file=sys.stderr)
        return 1
    print("Created and verified one annotated tag in this repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
