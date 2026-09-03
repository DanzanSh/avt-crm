from fulfil.models.product import Product
from fulfil.services.products import generate_internal_barcode, validate_gtin


def test_validate_gtin_accepts_known_lengths():
    assert validate_gtin("12345678")  # 8
    assert validate_gtin("123456789012")  # 12
    assert validate_gtin("1234567890123")  # 13
    assert validate_gtin("12345678901231")  # 14, не начинается с 0


def test_validate_gtin_rejects_bad_input():
    assert not validate_gtin("abc")
    assert not validate_gtin("123")  # неверная длина
    assert not validate_gtin("01234567890123")  # 14 цифр с ведущим нулём — тот же баг эталона


def test_generate_internal_barcode_format(db):
    p = Product(barcode="TEMP", name="Товар без ШК")
    db.add(p)
    db.commit()
    db.refresh(p)

    code = generate_internal_barcode(db, p)
    assert code == f"LDX-{p.id:06d}"
    assert p.barcode == code
