"""Test double: the retired all-GraphQL client's whole-repo `issue_list` (v0.4.3 removed it
from the plugin). Command tests that build the base `GitHub` client with a `ScriptedRunner` get it
through the `graphql_listing` fixture; production reads only what a command reaches (`GitHubRest`)."""
import sdlc_next
from sdlc_next import without_retro_issues

ISSUE_LIST_QUERY = """query {{ repository(owner:"__OWNER__", name:"__NAME__") {{
  issues(first: 100, after: {after}, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      number title body createdAt updatedAt state
      labels(first: 20) {{ nodes {{ name }} }}
      issueType {{ name }}
      parent {{ number }}
      milestone {{ title }}
      issueFieldValues(first: 10) {{
        nodes {{
          __typename
          ... on IssueFieldSingleSelectValue {{ field {{ ... on IssueFieldSingleSelect {{ name }} }} name }}
        }}
      }}
    }}
  }}
}} }}"""


ISSUE_LIST_QUERY = ISSUE_LIST_QUERY.replace("__OWNER__", sdlc_next._OWNER).replace(
    "__NAME__", sdlc_next._NAME)


def listed_issue_list(self) -> list:
    issues = []
    after = "null"
    while True:
        data = self.graphql(ISSUE_LIST_QUERY.format(after=after))
        conn = data["repository"]["issues"]
        for node in conn["nodes"]:
            node["labels"] = node["labels"]["nodes"]
            node["milestone"] = (node.get("milestone") or {}).get("title")
            node["fields"] = {
                v["field"]["name"]: v["name"]
                for v in node.pop("issueFieldValues")["nodes"]
                if v["__typename"] == "IssueFieldSingleSelectValue"
            }
            issues.append(node)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = f'"{conn["pageInfo"]["endCursor"]}"'
    return without_retro_issues(issues)


import pytest  # noqa: E402


@pytest.fixture
def graphql_listing(monkeypatch):
    """Give the base `GitHub` client the old whole-repo listing for one test module."""
    monkeypatch.setattr(sdlc_next.GitHub, "issue_list", listed_issue_list)
