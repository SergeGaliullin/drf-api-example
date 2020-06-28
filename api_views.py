from datetime import date

from rest_framework import status
from rest_framework.generics import CreateAPIView, UpdateAPIView, DestroyAPIView, ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import BelongsToUser
from .models import Weight
from .serializers import WeightSerializer, ApiSignS3PutRequestViewSerializer, \
    UnverifiedWeightSerializer, UpdateWeightSerializer, VideoVerificationSerializer


class CreateWeighinAPIView(APIView):
    queryset = Weight.objects.all()
    serializer_class = WeightSerializer

    def post(self, request, *args, **kwargs):
        participant = request.user
        weighin_type = request.GET.get('weighin_type', False)
        weighin_serializer = WeightSerializer(data=request.data, partial=True, context={'user': participant})
        weighin_serializer.is_valid(raise_exception=True)
        if weighin_type == "self-reported":
            weighin_serializer.save(
                verified=False,
                method='web',
                participant_id=participant.id,
                date_of_measurement=date.today()
            )
        elif weighin_type == "verified":
            weighin_serializer.save(verified=True, method='web', participant_id=participant.id)
        return Response(weighin_serializer.data)


class DeleteWeightAPIView(DestroyAPIView):
    queryset = Weight.objects
    permission_classes = BelongsToUser,


class RetrieveWeighinAPIView(ListAPIView):
    serializer_class = WeightSerializer

    def get_queryset(self, *args, **kwargs):
        participant = self.request.user
        return Weight.existing.filter(participant=participant).order_by('-date_of_measurement', '-modified')


class ApiVideoVerificationView(CreateAPIView):
    serializer_class = VideoVerificationSerializer


class ApiSignS3PutRequestView(APIView):
    serializer_class = ApiSignS3PutRequestViewSerializer

    def get(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.GET, context={'user': request.user})
        serializer.is_valid(raise_exception=True)
        context = serializer.save()
        return Response(context, status=status.HTTP_200_OK)


class CreateUnverifiedWeighinAPIView(APIView):
    serializer_class = UnverifiedWeightSerializer

    def post(self, request, *args, **kwargs):
        weighin_type = request.GET.get('weighin_type', False)
        serializer = self.serializer_class(
            data=request.data,
            context={'user': request.user, 'weighin_type': weighin_type}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class UpdateWeight(UpdateAPIView):
    permission_classes = BelongsToUser,
    serializer_class = UpdateWeightSerializer
    queryset = Weight.objects.all()
