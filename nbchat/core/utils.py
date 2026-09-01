"""Utility helpers shared across the nbchat package.
Only a handful of functions from the legacy code are required for the
new modular structure.  They are copied verbatim to avoid having the
legacy module depend on the new one.
"""

# Lazy import helper
_client = None
_tools = None
_db_module = None
_config_module = None
_compressor_module = None
_monitoring_module = None
_whatsapp_agent_module = None


def lazy_import(module_name: str):
    """Import a module only when needed.

    The function mirrors the behaviour of the legacy ``lazy_import``.
    """
    global _client, _tools, _db_module, _config_module, _compressor_module, _whatsapp_agent_module, _monitoring_module

    if module_name == "nbchat.core.client":
        if _client is None:
            from nbchat.core.client import get_client
            _client = get_client
        return _client()

    elif module_name == "nbchat.tools":
        if _tools is None:
            from nbchat.tools import get_tools
            _tools = get_tools
        return _tools()

    elif module_name == "nbchat.core.db":
        if _db_module is None:
            import nbchat.core.db as db_module
            _db_module = db_module
        return _db_module

    elif module_name == "nbchat.core.config":
        if _config_module is None:
            import nbchat.core.config as config_module
            _config_module = config_module
        return _config_module

    elif module_name == "nbchat.core.compressor":
        if _compressor_module is None:
            import nbchat.core.compressor as compressor_module
            _compressor_module = compressor_module
        return _compressor_module

    elif module_name == "nbchat.core.monitoring":
        if _monitoring_module is None:
            import nbchat.core.monitoring as monitoring_module
            _monitoring_module = monitoring_module
        return _monitoring_module
    
    elif module_name == "nbchat.channels.whatsapp_agent":
        if _whatsapp_agent_module is None:
            import nbchat.channels.whatsapp_agent as wa_module
            _whatsapp_agent_module = wa_module
        return _whatsapp_agent_module

    else:
        raise ValueError(f"Unknown module {module_name}")