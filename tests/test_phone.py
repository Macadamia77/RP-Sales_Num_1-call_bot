import pytest

from callbot import phone


@pytest.mark.parametrize(
    "text, expected",
    [
        ("공이, 일이삼사에 오육칠팔이요.", "0212345678"),
        ("아, 거기는 공이, 일이삼사에 오육칠팔로 해 보세요.", "0212345678"),
        ("010-1234-5678이요", "01012345678"),
        ("공일공 일이삼사 오육칠팔", "01012345678"),
        ("0212345678", "0212345678"),
        ("공이 칠 팔 구 일 이 삼 사", "027891234"),
        ("관리사무소는 공이 칠팔구 일이삼사 이에요", "027891234"),
        ("관리실 번호가 공삼일 이삼사 오육칠팔인데요", "0312345678"),
        ("1588-1234로 하세요", "15881234"),
        ("영이 일이삼사 오육칠팔번이요", "0212345678"),
        ("공이 국번 일이삼사 다음에 오육칠팔", "0212345678"),
    ],
)
def test_extract_single(text, expected):
    nums = phone.extract_numbers(text)
    assert [n.digits for n in nums] == [expected]
    assert nums[0].valid


def test_no_false_positive_on_normal_words():
    assert phone.extract_numbers("일이 있어서요 이 번호로 오세요") == []
    assert phone.extract_numbers("관리사무소는 사무실 이층에 있어요") == []


def test_local_number_needs_area_code():
    (n,) = phone.extract_numbers("일이삼사 오육칠팔이요")
    assert n.digits == "12345678"
    assert n.needs_area_code and not n.valid


def test_roles_are_split():
    nums = phone.extract_numbers("부동산은 031-111-2222고 관리사무소는 031-333-4444예요")
    assert [(n.digits, n.role) for n in nums] == [
        ("0311112222", "real_estate"),
        ("0313334444", "management_office"),
    ]


def test_area_code_answer():
    assert phone.parse_area_code("공이요") == "02"
    assert phone.parse_area_code("공삼일이요") == "031"
    assert phone.parse_area_code("031이에요") == "031"
    assert phone.parse_area_code("모르겠어요") is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("마지막은 팔이 아니라 구예요", "0212345679"),
        ("일이삼사가 아니고 일이삼오예요", "0212355678"),
        ("끝자리가 구예요", "0212345679"),
        ("마지막 네 자리는 오육칠구예요", "0212345679"),
    ],
)
def test_correction(text, expected):
    assert phone.apply_correction(text, "0212345678") == expected


def test_readback_is_generated_from_digits():
    assert phone.readback("0212345678") == "공 이, 일 이 삼 사, 오 육 칠 팔"
    assert phone.readback_display("01012345678") == "공 일 공 / 일 이 삼 사 / 오 육 칠 팔"


def test_classify_keeps_leading_zero_and_does_not_guess():
    assert phone.classify("0212345678").formatted == "02-1234-5678"
    assert not phone.classify("021234567890").valid
    assert phone.classify("1234567").needs_area_code
