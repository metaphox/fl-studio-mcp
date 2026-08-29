"""Piano Roll tools for FL Studio - persistent note placement.

This module provides tools for creating, editing, and deleting notes in
FL Studio's piano roll. Unlike MIDI real-time note triggering, these
tools create persistent notes by communicating with FL Studio's Piano Roll
scripting API via JSON files.

Communication flow:
1. MCP server writes requests to mcp_request.json
2. Keystroke trigger (Cmd+Opt+Y) executes FL Studio's ComposeWithLLM script
3. Script reads JSON, modifies piano roll, exports state to piano_roll_state.json
"""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fl_studio_mcp.utils.fl_trigger import get_trigger, trigger_fl_studio

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _get_fl_scripts_dir() -> Path:
    """Get the FL Studio Piano Roll scripts directory."""
    system = platform.system()

    if system in ("Darwin", "Windows"):
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    else:
        # Linux fallback (FL Studio doesn't officially support Linux)
        base = Path.home() / ".fl-studio" / "Settings"

    scripts_dir = base / "Piano roll scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


def _get_request_file() -> Path:
    """Get the path to the MCP request JSON file."""
    return _get_fl_scripts_dir() / "mcp_request.json"


def _get_response_file() -> Path:
    """Get the path to the MCP response JSON file."""
    return _get_fl_scripts_dir() / "mcp_response.json"


def _get_state_file() -> Path:
    """Get the path to the piano roll state JSON file."""
    return _get_fl_scripts_dir() / "piano_roll_state.json"


def _write_request(request: dict | list) -> None:
    """Write a request to the MCP request file."""
    request_file = _get_request_file()

    # Read existing requests if any
    existing = []
    if request_file.exists():
        try:
            with open(request_file) as f:
                data = json.load(f)
                if isinstance(data, list):
                    existing = data
                elif isinstance(data, dict):
                    existing = [data]
        except (json.JSONDecodeError, IOError):
            existing = []

    # Append new request(s)
    if isinstance(request, list):
        existing.extend(request)
    else:
        existing.append(request)

    # Write back
    with open(request_file, "w") as f:
        json.dump(existing, f, indent=2)


def _clear_request_file() -> None:
    """Clear the request file."""
    request_file = _get_request_file()
    if request_file.exists():
        request_file.unlink()


def _read_state() -> dict | None:
    """Read the current piano roll state."""
    state_file = _get_state_file()
    if not state_file.exists():
        return None

    try:
        with open(state_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


RESPONSE_TIMEOUT_SECONDS = 5.0
RESPONSE_POLL_INTERVAL = 0.15


def _read_response() -> dict | None:
    """Read the response written by the FL Studio script."""
    response_file = _get_response_file()
    if not response_file.exists():
        return None

    try:
        with open(response_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _response_signature() -> tuple[float, int] | None:
    """Fingerprint the response file so a later write can be detected."""
    response_file = _get_response_file()
    if not response_file.exists():
        return None

    stat = response_file.stat()
    return (stat.st_mtime, stat.st_size)


def _await_response(previous: tuple[float, int] | None) -> dict | None:
    """Wait for the FL Studio script to write a new response."""
    deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if _response_signature() != previous:
            return _read_response()
        time.sleep(RESPONSE_POLL_INTERVAL)

    return None


def _pending_request_count() -> int:
    """Count requests still waiting to be consumed by FL Studio."""
    request_file = _get_request_file()
    if not request_file.exists():
        return 0

    try:
        with open(request_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return 0

    return len(data) if isinstance(data, list) else 1


def _describe_response(response: dict | None, keystroke: str) -> str:
    """Turn the FL Studio response into an actionable status string."""
    if response is None:
        return (
            f" WARNING: no response from FL Studio within {RESPONSE_TIMEOUT_SECONDS:.0f}s and"
            f" {_pending_request_count()} request(s) still queued - the notes were NOT written."
            f" Focus FL Studio with a piano roll open, then press {keystroke} or call"
            f" fl_trigger_script."
        )

    if response.get("status") != "success":
        return f" WARNING: FL Studio reported a failure: {json.dumps(response)}"

    added = response.get("notes_added", 0)
    deleted = response.get("notes_deleted", 0)
    return (
        f" FL Studio applied it: {added} added, {deleted} deleted."
        f" Note that fl_send_notes cannot target a channel - it always writes to whichever"
        f" piano roll is open, so a non-zero delete count on a channel you expected to be"
        f" empty means the write landed in the wrong piano roll."
    )


def _midi_to_note_name(midi: int) -> str:
    """Convert MIDI note number to note name."""
    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    note_name = note_names[midi % 12]
    octave = (midi // 12) - 1
    return f"{note_name}{octave}"


def _get_trigger_info(auto_trigger: bool) -> str:
    """Attempt to trigger FL Studio and return a status suffix string."""
    trigger = get_trigger()

    if not auto_trigger:
        return (
            f" Not triggered; {_pending_request_count()} request(s) queued."
            f" Press {trigger.keystroke} or call fl_trigger_script to apply."
        )
    if not trigger.is_supported:
        return f" Auto-trigger not supported on {trigger.platform}. Press the trigger key manually."

    previous = _response_signature()
    if not trigger_fl_studio():
        return f" Warning: Could not trigger FL Studio. Press {trigger.keystroke} manually."

    return _describe_response(_await_response(previous), trigger.keystroke)


def register_piano_roll_tools(mcp: FastMCP) -> None:
    """Register piano roll tools with the MCP server."""

    @mcp.tool()
    def fl_send_notes(
        notes: list[dict],
        mode: str = "add",
        auto_trigger: bool = True
    ) -> str:
        """Add or replace notes in the FL Studio piano roll.

        This creates persistent notes in the currently open piano roll pattern.
        Notes use quarter-note timing for simplicity.

        Args:
            notes: List of note objects with properties:
                   - midi (int): MIDI note number (60 = C4/Middle C)
                   - duration (float): Length in quarter notes (1.0 = quarter note)
                   - time (float, optional): Start position in quarter notes (default 0)
                   - velocity (float, optional): Velocity 0.0-1.0 (default 0.8)
            mode: "add" to add notes, "replace" to clear existing notes first
            auto_trigger: Whether to automatically trigger FL Studio (default True)

        Example notes:
            [
                {"midi": 60, "duration": 1.0, "time": 0},      # C4 quarter note at beat 0
                {"midi": 64, "duration": 1.0, "time": 0},      # E4 (chord with C4)
                {"midi": 67, "duration": 1.0, "time": 0},      # G4 (C major chord)
                {"midi": 60, "duration": 0.5, "time": 1.0},    # C4 eighth note at beat 1
            ]
        """
        if not notes:
            return "Error: No notes provided"

        # Validate notes
        for i, note in enumerate(notes):
            if "midi" not in note:
                return f"Error: Note {i} missing 'midi' field"
            if "duration" not in note:
                return f"Error: Note {i} missing 'duration' field"

            # Set defaults
            note.setdefault("time", 0)
            note.setdefault("velocity", 0.8)

        requests = []

        # If replace mode, clear first
        if mode == "replace":
            requests.append({"action": "clear"})

        # Add notes request
        requests.append({
            "action": "add_notes",
            "notes": notes
        })

        _write_request(requests)

        trigger_info = _get_trigger_info(auto_trigger)
        note_count = len(notes)
        note_summary = ", ".join(
            f"{_midi_to_note_name(n['midi'])}@{n.get('time', 0)}"
            for n in notes[:5]
        )
        if note_count > 5:
            note_summary += f", ... ({note_count - 5} more)"

        return f"Queued {note_count} note(s): {note_summary}.{trigger_info}"

    @mcp.tool()
    def fl_send_chord(
        midi_notes: list[int],
        time: float = 0,
        duration: float = 1.0,
        velocity: float = 0.8,
        auto_trigger: bool = True
    ) -> str:
        """Add a chord (multiple simultaneous notes) to the FL Studio piano roll.

        This is a convenience function for adding multiple notes at the same time
        with the same duration. For more control, use fl_send_notes.

        Args:
            midi_notes: List of MIDI note numbers (e.g., [60, 64, 67] for C major)
            time: Start position in quarter notes (default 0)
            duration: Length in quarter notes for all notes (default 1.0)
            velocity: Velocity 0.0-1.0 for all notes (default 0.8)
            auto_trigger: Whether to automatically trigger FL Studio

        Example - C major chord at beat 0:
            fl_send_chord([60, 64, 67], time=0, duration=1.0)

        Example - Am7 chord at beat 2:
            fl_send_chord([57, 60, 64, 67], time=2, duration=2.0)
        """
        if not midi_notes:
            return "Error: No MIDI notes provided"

        # Build chord notes with velocity included
        chord_notes = [
            {"midi": midi, "velocity": velocity}
            for midi in midi_notes
        ]

        request = {
            "action": "add_chord",
            "time": time,
            "duration": duration,
            "notes": chord_notes
        }

        _write_request(request)

        trigger_info = _get_trigger_info(auto_trigger)
        note_names = ", ".join(_midi_to_note_name(n) for n in midi_notes)
        return f"Queued chord [{note_names}] at beat {time}, duration {duration}.{trigger_info}"

    @mcp.tool()
    def fl_delete_notes(
        notes: list[dict],
        auto_trigger: bool = True
    ) -> str:
        """Delete specific notes from the FL Studio piano roll.

        Args:
            notes: List of notes to delete, matching by midi and time:
                   - midi (int): MIDI note number
                   - time (float): Start position in quarter notes
            auto_trigger: Whether to automatically trigger FL Studio

        Example:
            [{"midi": 60, "time": 0}, {"midi": 64, "time": 0}]
        """
        if not notes:
            return "Error: No notes specified for deletion"

        request = {
            "action": "delete_notes",
            "notes": notes
        }
        _write_request(request)

        trigger_info = _get_trigger_info(auto_trigger)
        return f"Queued deletion of {len(notes)} note(s).{trigger_info}"

    @mcp.tool()
    def fl_clear_piano_roll(auto_trigger: bool = True) -> str:
        """Clear all notes from the FL Studio piano roll.

        Args:
            auto_trigger: Whether to automatically trigger FL Studio
        """
        request = {"action": "clear"}
        _write_request(request)

        trigger_info = _get_trigger_info(auto_trigger)
        return f"Queued clear all notes.{trigger_info}"

    @mcp.tool()
    def fl_get_piano_roll_state() -> dict:
        """Get the current state of notes in the FL Studio piano roll.

        Returns a dictionary containing:
        - ppq: Pulses per quarter note (ticks per beat)
        - notes: List of all notes with their properties

        Note: This reads from the last exported state. Trigger FL Studio
        (Cmd+Opt+Y on macOS) to refresh the state file after making changes.
        """
        state = _read_state()

        if state is None:
            return {
                "error": "No piano roll state available. Make sure FL Studio's "
                         "ComposeWithLLM script has been run at least once."
            }

        # Add human-readable note names
        if "notes" in state:
            for note in state["notes"]:
                if "midi" in note:
                    note["note_name"] = _midi_to_note_name(note["midi"])

        return state

    @mcp.tool()
    def fl_clear_request_queue() -> str:
        """Clear any pending note requests without executing them.

        Use this if you want to cancel queued changes before triggering FL Studio.
        """
        _clear_request_file()
        return "Request queue cleared."

    @mcp.tool()
    def fl_trigger_script() -> str:
        """Manually trigger FL Studio to process pending note requests.

        This sends the keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows)
        to FL Studio to execute the ComposeWithLLM piano roll script.
        """
        trigger = get_trigger()

        if not trigger.is_supported:
            return f"Error: Auto-trigger not supported on {trigger.platform}"

        previous = _response_signature()

        if not trigger_fl_studio():
            return f"Failed to trigger FL Studio. Try pressing {trigger.keystroke} manually."

        return "Triggered FL Studio." + _describe_response(
            _await_response(previous), trigger.keystroke
        )

    @mcp.tool()
    def fl_get_piano_roll_info() -> dict:
        """Get information about the Piano Roll integration status.

        Returns platform info, file paths, and whether auto-triggering is supported.
        """
        trigger = get_trigger()

        return {
            "platform": trigger.platform,
            "auto_trigger_supported": trigger.is_supported,
            "trigger_keystroke": trigger.keystroke,
            "scripts_dir": str(_get_fl_scripts_dir()),
            "request_file": str(_get_request_file()),
            "state_file": str(_get_state_file()),
            "request_file_exists": _get_request_file().exists(),
            "state_file_exists": _get_state_file().exists(),
        }
