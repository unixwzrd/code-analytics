"""Exercise the visible shell controls before activating a grouped destination."""


def reveal_navigation(page, label):
    """Reveal an exact sidebar button without navigating or bypassing guards."""
    navigation = page.locator('nav[data-workflow-mounted="true"]')
    navigation.wait_for(state="attached")
    control = navigation.get_by_role("button", name=label, exact=True, include_hidden=True)
    control.wait_for(state="attached")
    toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    group = control.locator("xpath=ancestor::details[@data-workflow-group][1]")
    if group.count() and group.get_attribute("open") is None:
        group.locator(":scope > summary").click()
    control.wait_for(state="visible")
    return control
