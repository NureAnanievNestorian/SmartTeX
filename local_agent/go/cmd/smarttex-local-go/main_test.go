package main

import "testing"

func TestWorkspaceChangesSkipsLocalSmarttexArtifactsButKeepsContext(t *testing.T) {
	state := workspaceState{Files: map[string]string{
		".smarttex/cache/pdf-pages/page-001.jpg": "old-cache",
		".smarttex/local_workspace_state.json":   "old-state",
		".smarttex/context/project-brief.md":     "old-context",
		"template/main.typ":                      "old-main",
	}}
	current := map[string]workspaceFileSnapshot{
		".smarttex/context/project-brief.md": {Path: ".smarttex/context/project-brief.md", Hash: "new-context", IsText: true, Content: "updated"},
	}

	changes := workspaceChanges(state, current)
	seen := map[string]string{}
	for _, change := range changes {
		path, _ := change["path"].(string)
		action, _ := change["action"].(string)
		seen[path] = action
	}

	if seen[".smarttex/context/project-brief.md"] != "upsert" {
		t.Fatalf("expected context file to stay syncable, got changes: %#v", changes)
	}
	if seen["template/main.typ"] != "delete" {
		t.Fatalf("expected regular deleted source file to be synced as delete, got changes: %#v", changes)
	}
	if _, ok := seen[".smarttex/cache/pdf-pages/page-001.jpg"]; ok {
		t.Fatalf("local cache artifact must not be synced, got changes: %#v", changes)
	}
	if _, ok := seen[".smarttex/local_workspace_state.json"]; ok {
		t.Fatalf("local workspace state must not be synced, got changes: %#v", changes)
	}
}
