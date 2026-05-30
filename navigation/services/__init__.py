"""Navigation service package.

Keep this package initializer intentionally lightweight. Import concrete
services from their modules (for example ``navigation.services.preparation``)
to avoid loading the whole indexing stack during Django URL checks.
"""
