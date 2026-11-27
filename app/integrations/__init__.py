"""Clients for external services the backend talks to as a peer.

A module here knows one service's wire protocol and nothing about what the
application does with it: no models, no ``AppError``s, no settings beyond
what it is constructed with. Meaning is assigned by the service-layer code
that calls it, so two features sharing one service share its protocol
without sharing each other's rules.
"""
