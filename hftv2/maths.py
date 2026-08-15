from __future__ import annotations

from hftv2.types import Quote, Venue


def bps(numer: float, denom: float) -> float:
    if denom == 0:
        return 0.0
    return numer / denom * 10_000.0


def residual_bps(mexc: Quote, leader: Quote) -> float:
    return bps(mexc.mid - leader.mid, leader.mid)


def venue_name(venue: Venue) -> str:
    return venue.key
