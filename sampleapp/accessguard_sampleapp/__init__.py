"""A synthetic multi-tenant project-tracking API that stands in for a customer application.

It has its own authentication (``X-Api-Key``), its own response envelopes (``{"data": ...}``)
and its own access rules, and it shares no code with AccessGuard. The ``flawed`` variant
contains three documented access-control bugs; the ``fixed`` variant corrects them. All data
is synthetic.
"""
