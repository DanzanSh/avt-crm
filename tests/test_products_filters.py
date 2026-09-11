"""Фильтры каталога на странице «Товары» (feature.txt, п.3.1).

Списки значений независимы друг от друга — выбор бренда не сужает размеры, —
поэтому filter_options() не принимает уже выбранные фильтры и считается по
всему каталогу клиента.
"""

import datetime as dt

from conftest import make_client

from fulfil.services.products import create_product, filter_options, list_products


def _seed(db, client_id, rows):
    for barcode, name, size, color, brand in rows:
        create_product(
            db, client_id=client_id, barcode=barcode, name=name, size=size, color=color,
            brand=brand, actor="tester",
        )


def _names(products):
    return sorted(p.name for p in products)


def test_single_filter_narrows_list(db, seller):
    _seed(db, seller.id, [
        ("2000000000001", "Футболка", "M", "белый", "Nike"),
        ("2000000000002", "Худи", "L", "чёрный", "Puma"),
    ])

    assert _names(list_products(db, size="M")) == ["Футболка"]


def test_filters_combine_with_and(db, seller):
    _seed(db, seller.id, [
        ("2000000000001", "Футболка", "M", "белый", "Nike"),
        ("2000000000002", "Футболка", "M", "чёрный", "Nike"),
        ("2000000000003", "Футболка", "L", "белый", "Nike"),
    ])

    found = list_products(db, name="Футболка", size="M", color="белый")

    assert [p.barcode for p in found] == ["2000000000001"]


def test_filter_combines_with_client_and_search(db, seller):
    other = make_client(db, name="Второй клиент")
    _seed(db, seller.id, [("2000000000001", "Футболка", "M", "белый", "Nike")])
    _seed(db, other.id, [("2000000000002", "Футболка", "M", "белый", "Nike")])

    assert len(list_products(db, size="M")) == 2
    assert [p.client_id for p in list_products(db, size="M", client_id=seller.id)] == [seller.id]
    assert list_products(db, size="M", client_id=seller.id, search="Худи") == []


def test_archived_product_hidden_from_list_and_options(db, seller):
    _seed(db, seller.id, [
        ("2000000000001", "Футболка", "M", "белый", "Nike"),
        ("2000000000002", "Худи", "L", "чёрный", "Puma"),
    ])
    # delete_product() без истории удаляет товар физически, а нам нужен именно
    # архивный — архивируем напрямую.
    hoodie = list_products(db, name="Худи")[0]
    hoodie.archived_at = dt.datetime.now(dt.timezone.utc)
    db.commit()

    assert _names(list_products(db)) == ["Футболка"]
    assert filter_options(db)["brand"] == ["Nike"]

    assert _names(list_products(db, include_archived=True)) == ["Футболка", "Худи"]
    assert filter_options(db, include_archived=True)["brand"] == ["Nike", "Puma"]


def test_filter_options_skips_empty_and_null(db, seller):
    _seed(db, seller.id, [
        ("2000000000001", "Футболка", "M", "белый", "Nike"),
        ("2000000000002", "Худи", None, "", None),
    ])

    options = filter_options(db)

    assert options["size"] == ["M"]
    assert options["color"] == ["белый"]
    assert options["brand"] == ["Nike"]
    assert options["name"] == ["Футболка", "Худи"]


def test_filter_options_are_unique_and_sorted(db, seller):
    _seed(db, seller.id, [
        ("2000000000001", "Футболка", "M", "белый", "puma"),
        ("2000000000002", "Футболка", "M", "чёрный", "Nike"),
        ("2000000000003", "Худи", "L", "белый", "adidas"),
    ])

    options = filter_options(db)

    assert options["size"] == ["L", "M"]
    assert options["brand"] == ["adidas", "Nike", "puma"]  # регистронезависимая сортировка


def test_filter_options_scoped_to_client(db, seller):
    other = make_client(db, name="Второй клиент")
    _seed(db, seller.id, [("2000000000001", "Футболка", "M", "белый", "Nike")])
    _seed(db, other.id, [("2000000000002", "Худи", "L", "чёрный", "Puma")])

    assert filter_options(db, client_id=seller.id)["brand"] == ["Nike"]
    assert filter_options(db, client_id=other.id)["brand"] == ["Puma"]
    assert filter_options(db)["brand"] == ["Nike", "Puma"]
