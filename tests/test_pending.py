"""Deterministic selection tests with opaque requests and fake time."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from pending import PendingManager


SCOPE = ("onebot:instance-a", "group:one", "user:1")


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def publish(manager, scope=SCOPE, request=None, candidates=None):
    generation = manager.begin(scope)
    pending = manager.publish(scope, generation, request if request is not None else object(),
                              candidates if candidates is not None else [object(), object()])
    assert pending is not None
    return pending


def test_original_action_and_candidate_order_survive_one_time_choice():
    manager = PendingManager()
    request = {"action": "recent", "limit": 10, "query": "Player"}
    candidates = [object(), object()]
    pending = publish(manager, request=request, candidates=candidates)
    candidates.reverse()
    chosen_request, chosen_candidate = manager.consume(SCOPE, pending.code, 2)
    assert chosen_request is request
    assert chosen_request["action"] == "recent"
    assert chosen_candidate is candidates[0]
    with pytest.raises(ValueError):
        manager.consume(SCOPE, pending.code, 2)
    assert manager.publish(SCOPE, pending.generation, request, candidates) is None


@pytest.mark.parametrize("scope", [
    ("onebot:instance-a", "group:one", "user:2"),
    ("onebot:instance-a", "group:two", "user:1"),
    ("onebot:instance-b", "group:one", "user:1"),
])
def test_other_users_sessions_and_platforms_cannot_consume_or_cancel(scope):
    manager = PendingManager()
    first = publish(manager)
    second = publish(manager, scope)
    with pytest.raises(ValueError):
        manager.consume(scope, first.code, 1)
    assert not manager.cancel(scope, first.code)
    assert manager.consume(SCOPE, first.code, 1)[0] is first.request
    assert manager.consume(scope, second.code, 1)[0] is second.request


def test_begin_immediately_invalidates_old_list_before_network_finishes():
    manager = PendingManager()
    old = publish(manager)
    generation = manager.begin(SCOPE)
    with pytest.raises(ValueError):
        manager.consume(SCOPE, old.code, 1)
    assert manager.publish(SCOPE, old.generation, old.request, old.candidates) is None
    new = manager.publish(SCOPE, generation, {"action": "bind"}, ["new"])
    assert new is not None
    assert manager.consume(SCOPE, new.code, 1) == ({"action": "bind"}, "new")


def test_old_network_result_cannot_replace_new_results():
    manager = PendingManager()
    old_generation = manager.begin(SCOPE)
    new_generation = manager.begin(SCOPE)
    new = manager.publish(SCOPE, new_generation, "read-only", ["right"])
    assert manager.publish(SCOPE, old_generation, "bind", ["wrong"]) is None
    assert manager.consume(SCOPE, new.code, 1) == ("read-only", "right")


def test_generation_can_publish_only_once_and_empty_results_invalidate_it():
    manager = PendingManager()
    first = publish(manager)
    assert manager.publish(SCOPE, first.generation, "bind", ["wrong"]) is None
    assert manager.consume(SCOPE, first.code, 1)[0] is first.request
    generation = manager.begin(SCOPE)
    assert manager.publish(SCOPE, generation, "empty", []) is None
    assert manager.publish(SCOPE, generation, "late", ["wrong"]) is None


@pytest.mark.parametrize("index", [0, -1, 3, "1", True, 1.5])
def test_out_of_range_or_invalid_index_preserves_valid_request(index):
    manager = PendingManager()
    pending = publish(manager)
    with pytest.raises(ValueError, match="序号"):
        manager.consume(SCOPE, pending.code, index)
    assert manager.consume(SCOPE, pending.code, 1)[0] is pending.request


def test_expiry_boundary_and_no_generation_reuse_after_cleanup():
    clock = Clock()
    manager = PendingManager(ttl=120, clock=clock)
    pending = publish(manager)
    clock.now += 120
    with pytest.raises(ValueError):
        manager.consume(SCOPE, pending.code, 1)
    assert not manager.cancel(SCOPE, pending.code)
    replacement = manager.begin(SCOPE)
    assert replacement != pending.generation
    assert manager.publish(SCOPE, pending.generation, "old", ["wrong"]) is None
    assert manager.publish(SCOPE, replacement, "new", ["right"]) is not None


def test_unpublished_network_request_expires_and_publication_starts_selection_ttl():
    clock = Clock()
    manager = PendingManager(ttl=120, clock=clock)
    expired = manager.begin(SCOPE)
    clock.now += 120
    assert manager.publish(SCOPE, expired, "old", ["wrong"]) is None
    generation = manager.begin(SCOPE)
    clock.now += 90
    pending = manager.publish(SCOPE, generation, "new", ["right"])
    clock.now += 119
    assert manager.consume(SCOPE, pending.code, 1) == ("new", "right")


def test_cancel_is_scoped_single_use_and_does_not_cancel_replacement():
    manager = PendingManager()
    first = publish(manager)
    second = publish(manager)
    assert not manager.cancel(SCOPE, first.code)
    assert manager.cancel(SCOPE, second.code)
    assert not manager.cancel(SCOPE, second.code)
    with pytest.raises(ValueError):
        manager.consume(SCOPE, second.code, 1)
    assert manager.publish(SCOPE, second.generation, "late", ["wrong"]) is None


def test_capacity_evicts_oldest_without_resurrecting_its_network_generation():
    manager = PendingManager(max_entries=2)
    one = publish(manager)
    two = publish(manager, ("p", "s", "2"))
    three = publish(manager, ("p", "s", "3"))
    with pytest.raises(ValueError):
        manager.consume(SCOPE, one.code, 1)
    assert manager.publish(SCOPE, one.generation, "late", ["wrong"]) is None
    assert manager.consume(two.scope, two.code, 1)[0] is two.request
    assert manager.consume(three.scope, three.code, 1)[0] is three.request


def test_simultaneous_consume_only_succeeds_once():
    manager = PendingManager()
    pending = publish(manager)

    def choose(_):
        try:
            return manager.consume(SCOPE, pending.code, 1)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(choose, range(8)))
    assert sum(result is not None for result in results) == 1


def test_generation_check_does_not_consume_or_extend_and_expires_at_boundary():
    clock = Clock()
    manager = PendingManager(ttl=120, clock=clock)
    generation = manager.begin(SCOPE)
    assert manager.is_current(SCOPE, generation)
    assert not manager.is_current(("other", "session", "user"), generation)
    pending = manager.publish(SCOPE, generation, "bind", ["player"])
    assert manager.is_current(SCOPE, generation)
    assert manager.is_current(SCOPE, generation)
    assert manager.consume(SCOPE, pending.code, 1) == ("bind", "player")
    assert not manager.is_current(SCOPE, generation)
    executing = manager.begin(SCOPE)
    clock.now += 119
    assert manager.is_current(SCOPE, executing)
    clock.now += 1
    assert not manager.is_current(SCOPE, executing)


def test_new_operation_invalidates_selected_execution_generation():
    manager = PendingManager()
    pending = publish(manager, request="bind", candidates=["player"])
    manager.consume(SCOPE, pending.code, 1)
    execution = manager.begin(SCOPE)
    assert manager.is_current(SCOPE, execution)
    replacement = manager.begin(SCOPE)
    assert not manager.is_current(SCOPE, execution)
    assert manager.is_current(SCOPE, replacement)


def test_clear_invalidates_all_scopes_and_prevents_late_publication():
    manager = PendingManager()
    published = publish(manager)
    other_scope = ("p", "s", "another")
    network_generation = manager.begin(other_scope)
    manager.clear()
    manager.clear()
    assert not manager.is_current(SCOPE, published.generation)
    assert not manager.is_current(other_scope, network_generation)
    with pytest.raises(ValueError):
        manager.consume(SCOPE, published.code, 1)
    assert not manager.cancel(SCOPE, published.code)
    new_generation = manager.begin(other_scope)
    assert new_generation > network_generation
    assert manager.publish(other_scope, network_generation, "old", ["wrong"]) is None
    assert manager.publish(SCOPE, published.generation, "old", ["wrong"]) is None
    assert manager.publish(other_scope, new_generation, "new", ["right"]) is not None


def test_pending_configuration_requires_bounded_lifetime_and_capacity():
    for ttl in [0, -1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            PendingManager(ttl=ttl)
    for maximum in [0, -1, 1.5, True]:
        with pytest.raises(ValueError):
            PendingManager(max_entries=maximum)
