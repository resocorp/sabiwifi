from django.urls import path

from staff import views

app_name = 'staff'

urlpatterns = [
    path('', views.staff_list, name='list'),
    path('create/', views.staff_create, name='create'),
    path('<int:pk>/', views.staff_update, name='update'),
    path('<int:pk>/delete/', views.staff_delete, name='delete'),
]
