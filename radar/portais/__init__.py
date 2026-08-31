"""Adaptadores de portais de anúncios.

Cada portal vive no seu próprio módulo e expõe `coletar_paginas(...)`,
gerando dicts normalizados. Se um portal quebrar (mudança de layout),
o erro fica contido aqui — o resto do sistema continua operando com os
dados já coletados.
"""


class PortalIndisponivel(Exception):
    """Portal bloqueou o acesso ou mudou o layout — coleta abortada."""
