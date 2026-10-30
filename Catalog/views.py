from django.shortcuts import render

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticatedOrReadOnly

from .models import Tea, TeaImage
from .serializers import TeaImageSerializer, TeaSerializer

from django.utils import timezone
from datetime import timedelta
class TeaViewSet(viewsets.ModelViewSet):
    # prefetch_related убирает N+1 при сериализации вложенной галереи.
    queryset = Tea.objects.prefetch_related('images').all()
    serializer_class = TeaSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'])
    def available(self, request):
        available_teas = self.get_queryset().filter(availability=True)
        serializer = self.get_serializer(available_teas, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def unavailable(self, request):
        unavailable_teas = self.get_queryset().filter(availability=False)
        serializer = self.get_serializer(unavailable_teas, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def new(self, request):
        new_teas = self.get_queryset().filter(availability=True, created_at__gte=timezone.now() - timedelta(days=30))
        serializer = self.get_serializer(new_teas, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def available_old(self, request):
        old_teas = self.get_queryset().filter(availability=True, created_at__lt=timezone.now() - timedelta(days=90))
        serializer = self.get_serializer(old_teas, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TeaImageViewSet(viewsets.ModelViewSet):
    """Галерея сорта для десктопной админки (inline TeaImage в Django Admin).

    Список фильтруется по ``?tea=<id>``; создание — multipart с полями
    ``tea`` и ``image``. Пагинация не нужна: изображений у сорта немного.
    """

    serializer_class = TeaImageSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        queryset = TeaImage.objects.select_related('tea').order_by('id')
        tea_id = self.request.query_params.get('tea')
        if tea_id in (None, ''):
            return queryset
        try:
            tea_id = int(tea_id)
        except (TypeError, ValueError):
            return queryset.none()
        return queryset.filter(tea_id=tea_id)
    
    

