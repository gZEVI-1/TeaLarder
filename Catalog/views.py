from django.shortcuts import render

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Tea
from .serializers import TeaSerializer

from django.utils import timezone
from datetime import timedelta
class TeaViewSet(viewsets.ModelViewSet):
    queryset = Tea.objects.all()
    serializer_class = TeaSerializer

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
    
    

