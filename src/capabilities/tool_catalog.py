"""Names of tools available to goal runs."""

SANDBOX_TOOL_NAMES = ("write", "read", "list", "shell", "calculate")

GOAL_TOOL_NAMES = (
    "ask_user", "declare_blocked", "schedule_job", "list_schedule",
    "cancel_schedule", "web_search", "fetch_url", "read_document",
    "search_document", "search_research_cache", "youtube_learn",
    "github_search", "github_fetch", "make_bar_chart", "make_line_chart",
    "make_chart", "generate_document", "build_ebook", "make_spreadsheet",
    "make_docx", "make_slides", "make_latex", "record_system_audio",
    "capture_webcam_photo", "record_webcam",
    "tts_speak", "transcribe_audio", "email_make_draft", "email_send",
    "slack_send", "whatsapp_send", "threads_publish", "threads_post",
    "instagram_publish", "instagram_post", "write_versioned", "read_file",
    "write_file", "notify", "sam_segment", "uv_venv",
    "install_python_package", "apt_install", "adopt_tool_plugin", "adopt_skill",
    "mcp_connect", "mcp_disconnect",
    "computer_use",
)


def all_tool_names(plugin_names=(), *, selfmod=False):
    """Return the deduplicated public tool list in stable order."""
    names = [*SANDBOX_TOOL_NAMES, *GOAL_TOOL_NAMES,
             *(str(name) for name in plugin_names)]
    if selfmod:
        names.append("self_modify")
    return list(dict.fromkeys(names))
