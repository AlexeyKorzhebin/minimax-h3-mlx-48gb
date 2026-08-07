#!/bin/bash
# Install the skill for every agent runtime present on this machine.
# ~/.agents/skills is the cross-runtime path Codex, Copilot CLI and Gemini CLI read;
# Claude Code reads ~/.claude/skills. Symlink both at the same source so there is one
# copy of the skill to maintain. Safe to run more than once.
set -eu
SRC="$(cd "$(dirname "$0")/../skills/generating-h3-video" && pwd)"

for target in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
  mkdir -p "$target"
  ln -sfn "$SRC" "$target/generating-h3-video"
  echo "installed -> $target/generating-h3-video"
done
