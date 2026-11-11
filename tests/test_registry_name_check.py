import norway_company_agent.identity as identity


def test_unregistered_competing_name_is_dropped_registered_one_kept(monkeypatch):
    registered = {"bergen consto": ["999999999"]}
    def lookup(name, own_org):
        key = " ".join(identity._tokens(name))
        if key == "unknown name":
            return None
        return any(org != own_org for org in registered.get(key, []))
    monkeypatch.setattr(identity, "registered_under_other_number", lookup)
    names = ["Bergen Consto AS", "Miljøfyrtårn Consto AS", "Unknown Name AS"]
    assert identity.registry_confirmed_competitors(names, "923456783") == ["Bergen Consto AS", "Unknown Name AS"]


def test_lookup_is_disabled_and_fails_closed_without_network(monkeypatch):
    monkeypatch.setenv("SIGNALPOST_REGISTRY_NAME_CHECK", "0")
    assert identity.registered_under_other_number("Bergen Consto AS", "923456783") is None
    assert identity.registry_confirmed_competitors(["Bergen Consto AS"], "923456783") == ["Bergen Consto AS"]


def test_only_8xx_9xx_series_are_organisation_numbers():
    from norway_company_agent.website_discovery import valid_org_number
    assert valid_org_number("984024290")
    assert not valid_org_number("171649366")
    assert not valid_org_number("000000000")
