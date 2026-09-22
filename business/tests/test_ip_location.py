"""IP 定位（客户端 IP 取用 + 公网判定 + 高德 ip 参数 + 前端分流标记）。

背景（线上缺陷）：手机在襄阳点「定位」，详细地址被自动填成
「北京市东城区交道口街道辛安里南锣鼓巷」。根因是 `amap_ip_location()`
调高德 `/v3/ip` **没传 `ip` 参数** —— 高德于是按「发起这次 HTTP 请求的一方」
定位，也就是**服务器机房**，与用户所在城市毫无关系。

本文件锁住修复后的四层约定：
  1. `client_ip()`   —— 只认 `X-Real-IP` / `REMOTE_ADDR`，**不认可伪造的 XFF**；
  2. `public_ipv4()` —— 内网/回环/CGNAT/IPv6/非法值一律判为「不可用」；
  3. `amap_ip_location(client_ip=...)` —— 可用才传 `ip`，并回传 `source` 标记；
  4. `geocode_ip` 视图 —— 把客户端 IP 传下去；`source=server_ip` 时给出
     「这是服务器所在城市」的提示（前端据此**不自动填表**）。
"""
import json as json_lib
from unittest import mock

from django.test import SimpleTestCase

from business.services import (
    amap_ip_location, client_ip, public_ipv4,
)
from business.tests.base import BusinessTestBase

GEOCODE_IP_URL = '/api/business/geocode/ip/'


class FakeRequest:
    """只带 META 的最小请求替身（`client_ip` 只读 META）。"""

    def __init__(self, **meta):
        self.META = meta


class ClientIpTest(SimpleTestCase):
    """`client_ip()`：取真实客户端 IP。"""

    def test_prefers_x_real_ip(self):
        """nginx 设的 `X-Real-IP $remote_addr` 是**覆盖式**的，优先采信。"""
        req = FakeRequest(HTTP_X_REAL_IP='114.247.50.2', REMOTE_ADDR='127.0.0.1')
        self.assertEqual(client_ip(req), '114.247.50.2')

    def test_falls_back_to_remote_addr(self):
        req = FakeRequest(REMOTE_ADDR='114.247.50.2')
        self.assertEqual(client_ip(req), '114.247.50.2')

    def test_none_request_returns_blank(self):
        self.assertEqual(client_ip(None), '')

    def test_missing_meta_returns_blank(self):
        self.assertEqual(client_ip(FakeRequest()), '')

    def test_ignores_x_forwarded_for(self):
        """⚠ 安全判据：`X-Forwarded-For` 客户端可任意伪造，**绝不能**采信。

        nginx 用的是 `$proxy_add_x_forwarded_for`（把客户端传进来的值
        原样追加在最前面），所以它的第一段就是「客户端说是谁就是谁」。
        若改用它，攻击者只要伪造一个 IP 就能操纵定位结果 —— 这条测试
        是防止后人「顺手优化」成 XFF。
        """
        req = FakeRequest(
            HTTP_X_FORWARDED_FOR='1.2.3.4',
            HTTP_X_REAL_IP='114.247.50.2',
            REMOTE_ADDR='127.0.0.1',
        )
        self.assertEqual(client_ip(req), '114.247.50.2')

    def test_xff_only_request_does_not_leak_into_result(self):
        """只有 XFF、没有 X-Real-IP 时，退回 REMOTE_ADDR，而不是 XFF。"""
        req = FakeRequest(HTTP_X_FORWARDED_FOR='1.2.3.4', REMOTE_ADDR='127.0.0.1')
        self.assertEqual(client_ip(req), '127.0.0.1')


class PublicIpv4Test(SimpleTestCase):
    """`public_ipv4()`：可用公网 IPv4 才返回，否则 None。"""

    def test_accepts_public_ipv4(self):
        for value in ('114.247.50.2', '223.5.5.5', '8.8.8.8'):
            with self.subTest(value=value):
                self.assertEqual(public_ipv4(value), value)

    def test_rejects_private_ranges(self):
        for value in ('10.0.0.5', '192.168.1.1', '172.16.0.1'):
            with self.subTest(value=value):
                self.assertIsNone(public_ipv4(value))

    def test_rejects_loopback_and_link_local(self):
        for value in ('127.0.0.1', '127.1.2.3', '169.254.1.1'):
            with self.subTest(value=value):
                self.assertIsNone(public_ipv4(value))

    def test_rejects_cgnat(self):
        """⚠ CGNAT（`100.64.0.0/10`）的 `is_private` 是 **False**。

        只判 `is_private` 会把它当成公网 IP 传给高德 —— 而高德查不到它，
        白白换来一个 400。Tailscale 就在这一段，生产服务器自身是
        `100.99.98.71`，走 Tailscale 访问时 `X-Real-IP` 正是 `100.x`。
        """
        for value in ('100.64.0.1', '100.99.98.71', '100.127.255.254'):
            with self.subTest(value=value):
                self.assertIsNone(public_ipv4(value))

    def test_rejects_multicast_reserved_and_unspecified(self):
        for value in ('224.0.0.1', '240.0.0.1', '0.0.0.0'):
            with self.subTest(value=value):
                self.assertIsNone(public_ipv4(value))

    def test_rejects_ipv6(self):
        """高德 `/v3/ip` 只支持国内 IPv4，IPv6 一律不传。"""
        for value in ('::1', '240e:1:2::1'):
            with self.subTest(value=value):
                self.assertIsNone(public_ipv4(value))

    def test_rejects_garbage_and_blank(self):
        for value in ('not-an-ip', '', None, '  ', '1.2.3.4.5', '999.1.1.1'):
            with self.subTest(value=repr(value)):
                self.assertIsNone(public_ipv4(value))

    def test_takes_first_segment_of_list(self):
        """X-Forwarded-For 形态（`a, b, c`）取第一段并去空白。"""
        self.assertEqual(public_ipv4('114.247.50.2, 10.0.0.1'), '114.247.50.2')
        self.assertEqual(public_ipv4('  114.247.50.2  '), '114.247.50.2')

    def test_list_whose_first_segment_is_private_is_rejected(self):
        """列表形态也不放过：第一段是内网就判不可用，不能跳到第二段。"""
        self.assertIsNone(public_ipv4('10.0.0.1, 114.247.50.2'))


class AmapIpLocationClientIpTest(SimpleTestCase):
    """`amap_ip_location(client_ip=...)`：可用客户端 IP 才写进 `ip` 参数。"""

    AMAP_OK = {
        'status': '1', 'info': 'OK', 'province': '湖北省', 'city': '襄阳市',
        'adcode': '420600', 'rectangle': '111.0,31.5;112.0,32.5',
    }

    def _patch(self, payload):
        patcher = mock.patch('business.services.urllib.request.urlopen')
        m_open = patcher.start()
        self.addCleanup(patcher.stop)
        resp = mock.Mock()
        resp.read.return_value = json_lib.dumps(payload).encode('utf-8')
        m_open.return_value.__enter__.return_value = resp
        return m_open

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_public_client_ip_is_sent_to_amap(self):
        """客户端公网 IP 必须出现在请求 URL 里 —— 这是「不再定位到机房」的判据。"""
        m_open = self._patch(self.AMAP_OK)
        out = amap_ip_location(client_ip='114.247.50.2')
        url = m_open.call_args[0][0].full_url
        self.assertIn('ip=114.247.50.2', url)
        self.assertEqual(out['source'], 'client_ip')
        self.assertEqual(out['city'], '襄阳市')

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_private_client_ip_is_not_sent(self):
        """内网客户端 IP 不能传 —— 高德只会返回空，等于白请求。"""
        m_open = self._patch(self.AMAP_OK)
        out = amap_ip_location(client_ip='192.168.1.7')
        url = m_open.call_args[0][0].full_url
        self.assertNotIn('ip=', url)
        self.assertEqual(out['source'], 'server_ip')

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_cgnat_client_ip_is_not_sent(self):
        """Tailscale 段同样不传（回归：曾只判 `is_private` 而漏掉 CGNAT）。"""
        m_open = self._patch(self.AMAP_OK)
        out = amap_ip_location(client_ip='100.99.98.71')
        self.assertNotIn('ip=', m_open.call_args[0][0].full_url)
        self.assertEqual(out['source'], 'server_ip')

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_missing_client_ip_is_not_sent(self):
        """完全拿不到客户端 IP 时退回服务器出口，并**如实标记**。"""
        m_open = self._patch(self.AMAP_OK)
        out = amap_ip_location()
        self.assertNotIn('ip=', m_open.call_args[0][0].full_url)
        self.assertEqual(out['source'], 'server_ip')

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_public_ip_without_rectangle_error_names_the_ip(self):
        """传了 IP 却查不到位置时，错误信息要**指出是哪个 IP**。

        否则用户只看到「服务器可能处于内网」这种指向服务器的话术，
        会误以为是自己网络的问题却无从下手。
        """
        self._patch({'status': '1', 'info': 'OK', 'province': [],
                     'city': [], 'rectangle': ''})
        with self.assertRaises(ValueError) as ctx:
            amap_ip_location(client_ip='114.247.50.2')
        self.assertIn('114.247.50.2', str(ctx.exception))

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    def test_server_fallback_without_rectangle_keeps_generic_message(self):
        """没传 IP 时的错误文案保持原样（不提 IP），避免误导。"""
        self._patch({'status': '1', 'info': 'OK', 'province': [],
                     'city': [], 'rectangle': ''})
        with self.assertRaises(ValueError) as ctx:
            amap_ip_location()
        self.assertIn('未获取到有效位置范围', str(ctx.exception))
        self.assertNotIn('114.247.50.2', str(ctx.exception))


class GeocodeIpViewSourceTest(BusinessTestBase):
    """`/api/business/geocode/ip/`：传客户端 IP 下去，并按 `source` 分流提示。"""

    def _mock_loc(self, **overrides):
        data = {
            'province': '湖北省', 'city': '襄阳市', 'adcode': '420600',
            'latitude': 32.0, 'longitude': 112.0, 'source': 'client_ip',
        }
        data.update(overrides)
        return data

    @mock.patch('business.views_capture.amap_regeo')
    @mock.patch('business.views_capture.amap_ip_location')
    def test_passes_x_real_ip_as_client_ip(self, m_ip, m_regeo):
        """视图必须把 nginx 给的 `X-Real-IP` 传给服务层 —— 修复的核心链路。"""
        m_ip.return_value = self._mock_loc()
        m_regeo.return_value = {
            'address': '湖北省襄阳市襄城区', 'province': '湖北省',
            'city': '襄阳市', 'district': '襄城区',
        }
        self.login_as(self.shelter_user_a)
        resp = self.client.get(GEOCODE_IP_URL, HTTP_X_REAL_IP='114.247.50.2')
        self.assertEqual(resp.status_code, 200, resp.content)
        m_ip.assert_called_once_with(client_ip='114.247.50.2')

    @mock.patch('business.views_capture.amap_regeo')
    @mock.patch('business.views_capture.amap_ip_location')
    def test_client_ip_source_returns_normal_message(self, m_ip, m_regeo):
        m_ip.return_value = self._mock_loc()
        m_regeo.return_value = {
            'address': '湖北省襄阳市襄城区', 'province': '湖北省',
            'city': '襄阳市', 'district': '襄城区',
        }
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(GEOCODE_IP_URL))
        self.assertEqual(body['data']['source'], 'client_ip')
        self.assertNotIn('服务器所在城市', body['message'])

    @mock.patch('business.views_capture.amap_regeo')
    @mock.patch('business.views_capture.amap_ip_location')
    def test_server_ip_source_warns_instead_of_pretending(self, m_ip, m_regeo):
        """⚠ 退回服务器出口时必须**明说**，前端据此不自动填表。

        这正是线上缺陷的现场：襄阳的手机拿到了「北京市东城区…」，
        而接口给的是一条「IP定位成功」的成功文案，前端于是照填。
        """
        m_ip.return_value = self._mock_loc(source='server_ip')
        m_regeo.return_value = {
            'address': '北京市东城区交道口街道', 'province': '北京市',
            'city': '北京市', 'district': '东城区',
        }
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(GEOCODE_IP_URL))
        self.assertEqual(body['data']['source'], 'server_ip')
        self.assertIn('服务器所在城市', body['message'])

    @mock.patch('business.views_capture.amap_regeo',
                side_effect=ValueError('地图服务请求失败：超时'))
    @mock.patch('business.views_capture.amap_ip_location')
    def test_source_survives_regeo_failure(self, m_ip, m_regeo):
        """逆地理失败走降级分支时，`source` 不能丢。

        丢了前端就分不清「这是你」还是「这是服务器」，又回到乱填。
        """
        m_ip.return_value = self._mock_loc(source='server_ip')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(GEOCODE_IP_URL))
        self.assertEqual(body['data']['source'], 'server_ip')
        self.assertEqual(body['data']['address'], '')

    @mock.patch('business.views_capture.amap_regeo')
    @mock.patch('business.views_capture.amap_ip_location')
    def test_no_header_falls_back_to_remote_addr(self, m_ip, m_regeo):
        """没有 `X-Real-IP` 时退回 `REMOTE_ADDR`（测试客户端是 127.0.0.1）。"""
        m_ip.return_value = self._mock_loc(source='server_ip')
        m_regeo.return_value = {
            'address': '', 'province': '', 'city': '', 'district': '',
        }
        self.login_as(self.shelter_user_a)
        self.ok(self.get_json(GEOCODE_IP_URL))
        m_ip.assert_called_once_with(client_ip='127.0.0.1')
