from app.wa_format import to_whatsapp


def test_markdown_a_whatsapp():
    src = (
        "1. **DoyPack papel 18x26**\n"
        "   Precio: $32.161,09\n"
        "   [Ver producto](https://ebags.com.ar/productos/doy-pack/)\n"
        "### Opciones\n"
        "__listo__"
    )
    out = to_whatsapp(src)
    assert "**" not in out and "](" not in out and "###" not in out
    assert "*DoyPack papel 18x26*" in out
    assert "Ver producto: https://ebags.com.ar/productos/doy-pack/" in out
    assert "*Opciones*" in out and "*listo*" in out


def test_link_con_la_url_de_texto_y_texto_normal_intacto():
    assert (
        to_whatsapp("[https://a.com/x](https://a.com/x)") == "https://a.com/x"
    )
    plain = "Hola 😊 ¿cuántas unidades necesitás? 2*3 = 6"
    assert to_whatsapp(plain) == plain
