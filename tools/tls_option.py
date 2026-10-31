"""Opção --no-strict-tls dos geradores em tools/ (mesma regra do app).

Em redes com proxy que intercepta o HTTPS (comum em redes corporativas), a CA do proxy
não tem a extensão Authority Key Identifier e o Python 3.13+ a recusa por
padrão (verificação estrita, VERIFY_X509_STRICT). O app resolve isso com
`[network] strict_tls = false` (radClock/core/net.py); os geradores não
importam o radClock, então repetem aqui a mesma regra em poucas linhas.
`./radClock.sh data` lê o `strict_tls` do config e passa a opção sozinho.

Uso em cada gerador:
    tls_option.add_argument(parser)                 # em parse_arguments()
    tls_option.configure(not args.no_strict_tls)    # no início de main()
    urllib.request.urlopen(..., context=tls_option.context())

Com a opção, só a verificação estrita sai: a cadeia até uma CA instalada no
sistema e o nome do servidor continuam sendo conferidos.
"""
import ssl

# contexto SSL de todos os downloads; trocado só por configure(), no início
_context = ssl.create_default_context()


def add_argument(parser):
    """Acrescenta --no-strict-tls ao argparse do gerador."""
    parser.add_argument("--no-strict-tls", action="store_true",
                        help="aceita a CA de um proxy TLS sem Authority Key Identifier "
                             "(rede com proxy TLS); o mesmo que [network] strict_tls = false")


def configure(strict):
    """Escolhe a verificação dos downloads: True = padrão do Python."""
    global _context
    context = ssl.create_default_context()
    if not strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        print("TLS: verificação estrita desligada (--no-strict-tls)")
    _context = context


def context():
    """Contexto SSL para passar a urllib.request.urlopen(..., context=...)."""
    return _context
