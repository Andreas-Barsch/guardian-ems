from config_ui import _config_html
from history_ui import render_history_html
from maintenance_ui import render_maintenance_html
from module_information_ui import render_module_information_html
from timeline_ui import render_timeline_html


LABELS = ("Module &amp; Stack", "Konfiguration", "Maintenance-Logbuch",
          "Verlauf", "Zeitverläufe")


def test_all_five_pages_share_guardian_maintenance_header_and_navigation():
    pages = (
        render_module_information_html(configuration_path="configuration",
                                       maintenance_path="maintenance"),
        _config_html(),
        render_maintenance_html(configuration_path="configuration"),
        render_timeline_html(configuration_path="configuration",
                             maintenance_path="maintenance"),
        render_history_html(configuration_path="configuration",
                            maintenance_path="maintenance", timeline_path="timeline"),
    )
    for page in pages:
        assert "<h1>Guardian Maintenance</h1>" in page
        for label in LABELS:
            assert page.count(f">{label}</a>") == 1
        assert page.count('aria-current="page"') == 1
