"""Zeekr vehicle provider (read-only), built on the zeekr_ev_api library.

Kept import-light: nothing here imports zeekr_ev_api or requests. The
provider module is loaded lazily by ``zeekrmate.providers.factory``.
"""
