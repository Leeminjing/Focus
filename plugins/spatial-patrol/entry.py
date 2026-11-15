"""Register the optional file viewer and manual DOCX editor without Agent tools.

The manifest retains its existing identity so saved plugin configuration and
asset URLs keep working. Viewing files requires no vision model.
"""

from focus.plugins.schemas import PluginContext, PluginDeclaration


def build_plugin(context: PluginContext) -> PluginDeclaration:
    """File preview and the lazy editor runtime are provided through HTTP/assets."""
    return PluginDeclaration()
