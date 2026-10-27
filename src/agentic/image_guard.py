"""Image guard and vision tiers.

The fast tier is text-only: sending it images wastes tokens and
confuses it, so images are allowed only on the strong tier.
This guard is checked before an image-bearing call is made, not
after it fails, and it never silently drops the images — a
rejected call reports why.
"""

from __future__ import annotations


def allowed_tiers() -> set[str]:
    """Tiers that may receive images."""
    return {"strong"}


def image_allowed(tier: str, has_image: bool) -> bool:
    """True when a call may proceed: no image, or a strong-tier call."""
    if not has_image:
        return True
    return tier in allowed_tiers()


def guard_image_input(tier: str, images) -> tuple[bool, str]:
    """Check an image input: (ok, reason). Reason is empty when ok."""
    count = len(images) if images is not None else 0
    if count == 0:
        return True, ""
    if tier in allowed_tiers():
        return True, ""
    return False, (f"tier {tier!r} is text-only: {count} image(s) "
                   f"require the strong tier")
