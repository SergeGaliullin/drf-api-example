import base64
import hashlib
import hmac
import json
import os
import time

from .conf import settings
from .utils import timezone
from rest_framework import serializers
from sorl.thumbnail import get_thumbnail

from core.serializers import DynamicFieldsModelSerializer
from .settings.base import MAIN_COMPETITION
from .models import Weight, WeightDeviceData
from .tasks import celery_send_congratulations_email
from .utils import convert_weight_to_kgs_and_lbs, validate_competitor, \
    validate_weight, validate_date, validate_height


class WeightSerializer(DynamicFieldsModelSerializer):
    date_of_measurement = serializers.DateField(validators=[validate_date])
    weight = serializers.SerializerMethodField()
    challenge_id = serializers.ReadOnlyField()

    def create(self, validated_data):
        weight_kilograms, weight_in_pounds = convert_weight_to_kgs_and_lbs(
            validated_data['weight_unit'],
            validated_data['weight']
        )

        return Weight.objects.create(
            participant=self.context['user'],
            verified=False,
            method='mobile',
            date_of_measurement=validated_data['date_of_measurement'],
            weight_kilograms=weight_kilograms,
            weight_in_pounds=weight_in_pounds,
        )

    def validate(self, data):
        user = self.context['user']
        weight = float(self.initial_data.get('weight'))
        date = data.get('date_of_measurement')
        data['user'] = user
        data['weight_unit'] = user.weight_unit
        data['weight'] = validate_weight(user, weight, date)
        return data

    def get_weight(self, instance):
        return round(instance.weight(), 2)

    class Meta:
        model = Weight
        exclude = ('medium',)


class WeightDeviceDataSerializer(DynamicFieldsModelSerializer):
    date_of_measurement_format = serializers.SerializerMethodField()
    value_format = serializers.SerializerMethodField()

    class Meta:
        model = WeightDeviceData
        fields = '__all__'

    def get_date_of_measurement_format(self, instance):
        return instance.date_of_measurement.strftime('%b %d, %Y')

    def get_value_format(self, instance):
        return format(int(instance.value), ',')


class VideoVerificationSerializer(serializers.Serializer):
    weight = serializers.FloatField(required=True)
    date = serializers.DateField(required=True, source='date_of_measurement', validators=[validate_date])
    s3_file_url = serializers.CharField(max_length=500, required=False)
    video_url = serializers.CharField(max_length=500, required=False)
    competitor_ids = serializers.CharField(max_length=100, required=False)
    user = serializers.HiddenField(default=serializers.CurrentUserDefault())
    platform = serializers.CharField(default='mobile')

    def create(self, validated_data):
        weight_kilograms, weight_in_pounds = convert_weight_to_kgs_and_lbs(
            validated_data['weight_unit'],
            validated_data['weight']
        )

        measurement = Weight.objects.create(
            participant=validated_data['user'],
            verified=True,
            method=validated_data['platform'],
            date_of_measurement=validated_data['date_of_measurement'],
            weight_kilograms=weight_kilograms,
            weight_in_pounds=weight_in_pounds,
        )

        if validated_data['s3_file_url']:
            measurement.verifier_file = validated_data['s3_file_url']
        else:
            measurement.video_link = validated_data['video_url']

        if validated_data['competitors']:
            teammate_id_list = [teammate.id for teammate in validated_data['competitors']]
            measurement.competitors.add(*teammate_id_list)

            challenges_id_list = [teammate.team.challenge.id for teammate in validated_data['competitors']]
            if MAIN_COMPETITION in challenges_id_list:
                celery_send_congratulations_email.delay(measurement.id)

        measurement.save()
        return measurement

    def validate(self, data):
        user = self.context['request'].user
        weight = float(data.get('weight'))
        date = data.get('date_of_measurement')
        competitor_ids = data.get('competitor_ids', None)
        data['user'] = user
        data['weight_unit'] = user.weight_unit
        data['competitors'] = validate_competitor(competitor_ids)
        data['weight'] = validate_weight(user, weight, date)
        data['s3_file_url'] = data.get('s3_file_url', None)
        data['video_url'] = data.get('video_url', None)

        if not data['s3_file_url'] and not data['video_url']:
            raise serializers.ValidationError("Please attach a video file or include a video link.")
        return data


class ApiSignS3PutRequestViewSerializer(serializers.Serializer):
    weight = serializers.FloatField(required=True)
    height_big = serializers.IntegerField(required=True)
    height_small = serializers.IntegerField(required=True)
    date = serializers.DateField(required=True)
    video_filename = serializers.CharField(max_length=1000, required=True)
    competitor_ids = serializers.CharField(max_length=100, required=False)

    def create(self, validated_data):
        file_name = validated_data['video_filename']
        user = validated_data['user']
        file_name, file_extension = os.path.splitext(file_name)
        file_name = base64.b64encode(file_name.encode('utf-8')).decode('utf-8')
        file_name = file_name + file_extension

        expires = int(time.time() + 1000)
        object_name = hashlib.sha1(str(expires).encode('utf-8')).hexdigest() + f'-{user.special_code}-{file_name}'

        conditions = [
            {'bucket': settings.S3_BUCKET_NAME},
            ['starts-with', '$key', object_name],
            {'acl': 'public-read'},
            {'success_action_status': '201'},
            ["starts-with", "$Content-Type", ""]
        ]

        policy_document = {
            "expiration": (timezone.now() + timezone.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "conditions": conditions
        }

        policy_document = json.dumps(policy_document, indent=2)
        policy = base64.b64encode(policy_document.encode('utf-8'))
        signature = base64.encodestring(hmac.new(settings.S3_SECRET_KEY.encode('utf-8'), policy, hashlib.sha1).digest())
        bucket_url = f'https://{settings.S3_BUCKET_NAME}.s3.amazonaws.com/'
        file_url = f'https://{settings.S3_BUCKET_NAME}.s3.amazonaws.com/{object_name}'

        return {
            'bucket_url': bucket_url,
            's3_file_url': file_url,
            'key': object_name.strip(),
            'policy': policy.strip(),
            'signature': signature.strip(),
            'AWSAccessKeyId': settings.S3_ACCESS_KEY,
            'acl': 'public-read',
            'success_action_status': 201,
            'Content-Type': ''
        }

    def validate(self, data):
        user = self.context['user']
        date = data.get('date')
        competitor_ids = data.get('competitor_ids', None)
        height_big = data.get('height_big')
        height_small = data.get('height_small')
        validate_height(height_big, height_small, user)
        data['user'] = user
        data['competitor_ids'] = validate_competitor(competitor_ids)
        data['date'] = validate_date(date)
        data['video_filename'] = data.get('video_filename', None)
        return data


class UnverifiedWeightSerializer(serializers.Serializer):
    date_of_measurement = serializers.DateField(validators=[validate_date])
    weight = serializers.FloatField()
    weight_image = serializers.SerializerMethodField()

    def to_representation(self, instance):
        weigh_ins = Weight.existing.before_date(
            self.context['user'], instance['date_of_measurement']
        ).order_by('date_of_measurement')
        if weigh_ins.count() > 1:
            weigh_ins = weigh_ins.order_by('-created')
            first_entry = False
            new_weigh_in = weigh_ins.first()
            old_weigh_in = weigh_ins[1]
            if self.context['user'].weight_unit == "kg":
                weight_difference = old_weigh_in.weight_kilograms - new_weigh_in.weight_kilograms
            else:
                weight_difference = old_weigh_in.weight_in_pounds - new_weigh_in.weight_in_pounds

            if weight_difference >= 0:
                lost_weight = 'lost'
            else:
                lost_weight = 'gained'
        else:
            first_entry = True
            weight_difference = 0
            lost_weight = 0
        return {
            'first_entry': first_entry,
            'weight_difference': abs(weight_difference),
            'lost_weight': lost_weight,
            'weight_unit': self.context['user'].weight_unit
        }

    def save(self, **kwargs):
        weight_kilograms, weight_in_pounds = convert_weight_to_kgs_and_lbs(
            self.validated_data['weight_unit'],
            self.validated_data['weight']
        )

        return Weight.objects.create(
            participant=self.context['user'],
            verified=False,
            method='mobile',
            date_of_measurement=self.validated_data['date_of_measurement'],
            verifier_file=self.initial_data.get('weight_image', None),
            weight_kilograms=weight_kilograms,
            weight_in_pounds=weight_in_pounds,
        )

    def validate(self, data):
        user = self.context['user']
        weight = float(self.initial_data.get('weight'))
        date_of_measurement = data.get('date_of_measurement')
        self._validate_weigh_in_type(self.context.get('weighin_type', False))
        validate_weight(user=user, new_weight=weight, new_date=date_of_measurement)
        data['weight'] = weight
        data['weight_unit'] = user.weight_unit
        return data

    def _validate_weigh_in_type(self, weigh_in_type):
        if not weigh_in_type:
            raise serializers.ValidationError("Weigh-in type is required")
        if weigh_in_type not in ['self-reported', 'verified']:
            raise serializers.ValidationError("Invalid weigh-in type")

    def get_weight_image(self, weight):
        image_name = weight.get('weight_image', None)
        if image_name:
            image_name = str(image_name)
            extension = os.path.splitext(image_name)[1][1:].strip().lower()
            if extension in ['jpg', 'jpeg', 'png']:
                if '.png' in image_name:
                    return get_thumbnail(
                        weight.verifier_file, '200x200', format='PNG', THUMBNAIL_CACHE_TIMEOUT=None).url
                else:
                    return get_thumbnail(weight.verifier_file, '200x200', THUMBNAIL_CACHE_TIMEOUT=None).url
            else:
                raise serializers.ValidationError(
                    "Images must be in PNG or JPG or JPEG format, and no larger than 10MB in size"
                )


class UpdateWeightSerializer(serializers.Serializer):
    date_of_measurement = serializers.DateField(required=True, validators=[validate_date])
    weight = serializers.FloatField(required=True)
    weight_image = serializers.FileField(source='verifier_file', required=False)
    remove_image = serializers.BooleanField(required=False)

    def update(self, instance, validated_data):
        weight_kilograms, weight_in_pounds = convert_weight_to_kgs_and_lbs(
            validated_data['weight_unit'],
            validated_data['weight']
        )

        self.instance.date_of_measurement = validated_data['date_of_measurement']
        self.instance.weight_kilograms = weight_kilograms
        self.instance.weight_in_pounds = weight_in_pounds
        weight_image = self.context['request'].data.get('weight_image', None)
        if weight_image:
            self.instance.verifier_file = weight_image
        else:
            if validated_data.get('remove_image', None):
                self.instance.verifier_file = None

        self.instance.save()
        return self.instance

    def validate(self, data):
        user = self.context['request'].user
        date_of_measurement = data.get('date_of_measurement')
        weight = float(data.get('weight'))
        self._image_has_correct_format(self.context['request'].data.get('weight_image', None))
        validate_weight(user, weight, date_of_measurement, update=True)
        data['weight_unit'] = user.weight_unit
        return data

    def _image_has_correct_format(self, image):
        if image:
            image_format = os.path.splitext(image.name)[1][1:].strip().lower()
            if image_format not in ['jpg', 'jpeg', 'png']:
                raise serializers.ValidationError("Images must be in PNG or JPG or JPEG format")
