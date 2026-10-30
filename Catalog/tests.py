"""Тесты API, которые использует десктопная админка.

Проверяем расширения, добавленные для паритета с Django Admin:
запись ``created_at``, очистку фото, CRUD галереи ``TeaImage`` и
вложенную выдачу изображений в сорте.
"""

import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from Catalog.models import Tea, TeaImage


def png_upload(name: str, color: str = "red") -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.InMemoryStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class TeaAdminApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="api-tester", password="secret"
        )
        self.client.force_authenticate(self.user)
        self.tea = Tea.objects.create(
            name="Тестовый сорт",
            place_of_purchase="Тестовый магазин",
        )

    def test_created_at_can_be_changed(self):
        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"created_at": "2020-05-06T07:08:00Z"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.tea.refresh_from_db()
        self.assertEqual(self.tea.created_at.year, 2020)
        self.assertEqual(self.tea.created_at.month, 5)

    def test_photo_can_be_uploaded_and_cleared(self):
        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"photo": png_upload("photo.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["photo"])
        self.tea.refresh_from_db()
        self.assertTrue(self.tea.photo)

        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"photo": None},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["photo"])
        self.tea.refresh_from_db()
        self.assertFalse(self.tea.photo)

    def test_gallery_crud_and_nested_images(self):
        response = self.client.post(
            "/api/tea-images/",
            {"tea": self.tea.pk, "image": png_upload("gallery.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        image_id = response.data["id"]
        self.assertEqual(TeaImage.objects.filter(tea=self.tea).count(), 1)

        response = self.client.get(
            "/api/tea-images/", {"tea": self.tea.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], image_id)

        response = self.client.get(f"/api/teas/{self.tea.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["images"]), 1)

        response = self.client.delete(f"/api/tea-images/{image_id}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(TeaImage.objects.filter(tea=self.tea).count(), 0)

    def test_anonymous_cannot_write(self):
        client = APIClient()
        response = client.post(
            "/api/teas/",
            {
                "name": "Аноним",
                "place_of_purchase": "Ниоткуда",
            },
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Tea.objects.filter(name="Аноним").exists())

    def test_delete_twice_returns_404(self):
        response = self.client.delete(f"/api/teas/{self.tea.pk}/")
        self.assertEqual(response.status_code, 204)

        response = self.client.delete(f"/api/teas/{self.tea.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_delete_cascades_gallery_rows_and_files(self):
        image = TeaImage.objects.create(
            tea=self.tea, image=png_upload("gallery.png")
        )
        storage = image.image.storage
        image_name = image.image.name
        self.assertTrue(storage.exists(image_name))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/teas/{self.tea.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Tea.objects.filter(pk=self.tea.pk).exists())
        self.assertFalse(TeaImage.objects.filter(pk=image.pk).exists())
        self.assertFalse(storage.exists(image_name))

    def test_delete_removes_main_photo_file(self):
        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"photo": png_upload("photo.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        self.tea.refresh_from_db()
        storage = self.tea.photo.storage
        photo_name = self.tea.photo.name
        self.assertTrue(storage.exists(photo_name))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/teas/{self.tea.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(storage.exists(photo_name))

    def test_availability_can_be_toggled(self):
        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"availability": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["availability"])
        self.tea.refresh_from_db()
        self.assertFalse(self.tea.availability)

        response = self.client.patch(
            f"/api/teas/{self.tea.pk}/",
            {"availability": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.tea.refresh_from_db()
        self.assertTrue(self.tea.availability)

    def test_anonymous_cannot_delete(self):
        client = APIClient()
        response = client.delete(f"/api/teas/{self.tea.pk}/")
        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Tea.objects.filter(pk=self.tea.pk).exists())
